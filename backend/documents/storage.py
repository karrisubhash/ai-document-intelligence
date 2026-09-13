"""Getting a real file on disk, whatever the storage backend is.

Every extractor in this pipeline needs a filesystem path, and that is not a
shortcut to be refactored away: poppler renders a PDF by path, tesseract
reads an image by path, and python-docx opens a ZIP by path. They are C
programs and subprocesses, not Python file objects.

What WAS a shortcut is how the path was obtained. `run_pipeline` read
`document_version.file.path`, which only exists on a filesystem storage -
`Storage.path()` raises NotImplementedError on S3, GCS, Azure and every
other remote backend. So the platform could not be pointed at object
storage at all, however it was configured: the very first stage would raise,
every document would fail, and §15's "S3-compatible object storage" was
unreachable from the code rather than merely unconfigured.

`local_copy` is the seam. On a filesystem storage it hands back the real
path and copies nothing. On a remote one it streams the object to a
temporary file, yields that, and deletes it afterwards - so the stages stay
exactly as they are, and the choice of backend stops being their problem.

The download is streamed rather than read into memory. A 50 MB upload is
the documented ceiling, and a worker that buffers several of those at once
while OCR is already resident is how a memory limit gets hit in production
rather than in testing.
"""

import contextlib
import logging
import os
import shutil
import tempfile

from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger("adi.storage")

# Streamed, not read whole. 1 MiB is large enough that the syscall overhead
# is irrelevant and small enough that concurrency does not multiply into
# real memory pressure.
COPY_CHUNK_BYTES = 1024 * 1024


def is_remote() -> bool:
    """Whether the active storage keeps files somewhere other than disk."""
    try:
        default_storage.path("probe")
    except NotImplementedError:
        return True
    except Exception:
        # A backend that objects to the probe name for some other reason is
        # still a local one - only NotImplementedError means "no paths".
        return False
    return False


def describe() -> str:
    """Human-readable backend name, for logs and the health endpoint."""
    backend = settings.STORAGES["default"]["BACKEND"]
    return backend.rsplit(".", 1)[-1]


@contextlib.contextmanager
def local_copy(file_field):
    """Yield a filesystem path for `file_field`, valid inside the block.

    On a local storage this is the stored file itself, so nothing is copied
    and nothing is deleted. On a remote storage it is a temporary file that
    is removed on the way out, including when the body raises - a pipeline
    that fails mid-OCR must not leave the document sitting in /tmp.
    """
    try:
        path = file_field.path
    except (NotImplementedError, AttributeError):
        path = None

    if path is not None:
        yield path
        return

    suffix = os.path.splitext(file_field.name)[1]
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temporary = handle.name

    try:
        with file_field.storage.open(file_field.name, "rb") as source:
            shutil.copyfileobj(source, handle, COPY_CHUNK_BYTES)
        handle.close()

        logger.info(
            "storage.materialized",
            extra={"file_name": file_field.name, "bytes": os.path.getsize(temporary)},
        )
        yield temporary
    finally:
        handle.close()
        try:
            os.unlink(temporary)
        except OSError as exc:  # pragma: no cover - platform-dependent
            # A leaked temp file is untidy; failing the job over it would
            # be worse, and the document has already been processed.
            logger.warning(
                "storage.temp_cleanup_failed",
                extra={"path": temporary, "error": str(exc)},
            )
