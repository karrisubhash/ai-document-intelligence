import hashlib

from django.db import models, transaction

from .models import Document, DocumentVersion, ProcessingJob
from .tasks import process_document_task


def _compute_checksum(file_obj) -> str:
    hasher = hashlib.sha256()
    for chunk in file_obj.chunks():
        hasher.update(chunk)
    file_obj.seek(0)
    return hasher.hexdigest()


def create_document_version(
    document: Document,
    file_obj,
    uploaded_by=None,
) -> DocumentVersion:
    """Attach a new, immutable version to a document and queue processing.

    Never reuses or overwrites a prior version's file - each upload,
    including reuploads of the "same" document, becomes a new version
    so the original artifact stays available as evidence.
    """
    next_version_number = (
        document.versions.aggregate(models.Max("version_number"))["version_number__max"]
        or 0
    ) + 1

    with transaction.atomic():
        version = DocumentVersion.objects.create(
            document=document,
            version_number=next_version_number,
            file=file_obj,
            file_name=file_obj.name,
            file_size=file_obj.size,
            mime_type=getattr(file_obj, "content_type", "") or "",
            checksum=_compute_checksum(file_obj),
            uploaded_by=uploaded_by,
        )

        document.current_version = version
        document.status = Document.Status.QUEUED
        document.save(update_fields=["current_version", "status", "updated_at"])

        job = ProcessingJob.objects.create(document_version=version)

    transaction.on_commit(lambda: process_document_task.delay(job.id))

    return version


def reprocess_document(document: Document) -> ProcessingJob:
    version = document.current_version

    if version is None:
        raise ValueError("Document has no current version to reprocess.")

    with transaction.atomic():
        document.status = Document.Status.QUEUED
        document.save(update_fields=["status", "updated_at"])

        job = ProcessingJob.objects.create(document_version=version)

    transaction.on_commit(lambda: process_document_task.delay(job.id))

    return job
