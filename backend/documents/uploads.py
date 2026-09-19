"""What the API accepts before anything is written to storage.

The pipeline already refuses an oversized, empty or unsupported file: the
INGESTION stage checks size and sniffs the content, and VALIDATION refuses a
type it cannot parse. Those checks are correct and they stay - content
sniffing is the only trustworthy answer to "what is this file", and it needs
the bytes on disk to do it.

What they cannot do is happen in time. They run in the Celery worker, which
is minutes after the request returned 202, and by then the upload has been
committed: a `Document` row, a `DocumentVersion` row, and the bytes
themselves written to MEDIA_ROOT or PUT into the S3 bucket. The document is
marked FAILED and every one of those artifacts stays. So an authenticated
caller could store arbitrary content of arbitrary size in the institution's
object store, indefinitely, and the only trace was a failed job - which is
what "unrestricted upload" means in practice, even though nothing the
platform *reads* was ever unrestricted.

This module is the admission check that runs first, in the request, before
`create_document_version` writes anything:

  * an empty file is refused,
  * a file over MAX_DOCUMENT_SIZE_BYTES is refused,
  * a file whose extension is not one the pipeline can parse is refused.

It is deliberately the cheap, deterministic half of the job. It does NOT
sniff content and does not try to: the extension is client-supplied and
proves nothing, so a `.pdf` that is not a PDF still reaches the pipeline and
is still caught there. The point is not to decide what the file is - it is
to stop the obviously-inadmissible from being persisted.

WHAT THIS DOES NOT BOUND. The request body has already been read by the time
a serializer runs: Django's multipart parser has spooled it to a temporary
file, which is then discarded. This bounds what gets STORED, not what gets
TRANSFERRED. Bounding the transfer is nginx's `client_max_body_size` (55m in
`infrastructure/nginx/nginx.conf`, deliberately just above the application
limit so the application produces the error message rather than the proxy).
A deployment without that proxy still reads the whole body before rejecting
it.
"""

import os

from django.conf import settings
from rest_framework import serializers, status
from rest_framework.exceptions import APIException

from documents.models import AuditLog
from documents.observability import telemetry
from documents.processing.stages.ingestion import EXTENSION_TYPES

from .audit import record_event

# One source of truth with the pipeline. EXTENSION_TYPES is the map INGESTION
# already uses to read a declared type off a filename, so a format added
# there is accepted here without a second list going stale.
SUPPORTED_EXTENSIONS = frozenset(EXTENSION_TYPES)


class PayloadTooLarge(APIException):
    """413, not 400.

    A validation error says the client sent something wrong; 413 says it
    sent something too big, which is a different thing to act on - the fix
    is to split or compress the document, not to correct a field. It also
    separates cleanly in logs from malformed requests, which matters when
    the question is whether someone is filling the bucket on purpose.
    """

    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "Uploaded file is too large."
    default_code = "file_too_large"


def max_upload_bytes() -> int:
    return settings.MAX_DOCUMENT_SIZE_BYTES


def _reject(user, reason, **detail):
    """Record a refusal, then let the caller raise.

    Refused uploads are audited as a DENIED create for the same reason
    authorization denials are: the interesting security signal is not that
    someone uploaded a document, it is that someone kept trying to upload
    something the platform will not take. The file NAME is recorded and its
    contents never are - a name is metadata a user chose, and it is what an
    operator needs to recognise a pattern.
    """
    record_event(
        user=user,
        action=AuditLog.Action.DOCUMENT_CREATE,
        result=AuditLog.Result.DENIED,
        reason=reason,
        **detail,
    )
    telemetry.upload_rejected(reason, user_id=getattr(user, "id", None))


def validate_upload(file_obj, *, user=None):
    """Refuse an upload that must not be stored. Returns it unchanged.

    Raises DRF exceptions, so it is usable directly as a serializer field
    validator: ValidationError (400) for something the caller got wrong,
    PayloadTooLarge (413) for something merely too big.
    """
    name = getattr(file_obj, "name", "") or ""
    size = getattr(file_obj, "size", 0) or 0

    if size <= 0:
        _reject(user, "empty", file_name=name)
        raise serializers.ValidationError("Uploaded file is empty.")

    limit = max_upload_bytes()
    if size > limit:
        _reject(user, "too_large", file_name=name, size_bytes=size, limit_bytes=limit)
        raise PayloadTooLarge(f"File is {size} bytes, over the {limit} byte limit.")

    extension = os.path.splitext(name)[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        _reject(user, "unsupported_extension", file_name=name, extension=extension)
        raise serializers.ValidationError(
            f"Unsupported file type '{extension or name}'. Supported: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
            + "."
        )

    return file_obj
