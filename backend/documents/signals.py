"""Reclaiming stored files when the rows that referenced them go away.

Django deletes rows, never files. `Document.objects.delete()` cascades
happily through versions, content, chunks, tables and figures and leaves
every uploaded byte on disk forever - so a platform whose whole purpose is
storing documents accumulates orphans that no longer appear in any listing,
cannot be reached through any endpoint, and are invisible to the audit log
that recorded the deletion as done.

That matters beyond disk usage. A deletion that leaves the document readable
to anyone with filesystem access is not a deletion, and "delete my document"
is a request this system is expected to honour (§14, and the audit log
already records DELETE as an action that happened).

Two details make this safe rather than merely tidy:

*After the commit, not during it.* post_delete fires inside the transaction.
Removing the file there means a transaction that later rolls back leaves the
row restored and the file gone - the one outcome worse than an orphan.
transaction.on_commit defers the unlink until the delete is durable.

*Never fatal.* A file that is already missing, or a storage backend that is
briefly unreachable, must not turn a successful deletion into a 500. The
row is gone either way; the worst case is an orphan, which is where this
started.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import DocumentVersion

logger = logging.getLogger("adi.storage")


@receiver(post_delete, sender=DocumentVersion)
def delete_stored_file(sender, instance, **kwargs):
    """Remove the uploaded artifact once its version row is really gone."""
    stored = instance.file
    if not stored:
        return

    name = stored.name
    storage = stored.storage

    def _remove():
        try:
            if storage.exists(name):
                storage.delete(name)
                logger.info("storage.file_deleted", extra={"file_name": name})
        except Exception as exc:  # pragma: no cover - storage-dependent
            # Deliberately broad: every storage backend raises something
            # different, and none of them should be able to fail a
            # deletion that has already committed.
            logger.warning(
                "storage.file_delete_failed",
                extra={"file_name": name, "error": str(exc)},
            )

    transaction.on_commit(_remove)
