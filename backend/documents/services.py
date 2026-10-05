import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .models import Document, DocumentVersion, ProcessingJob
from .processing.errors import ErrorCode
from .tasks import process_document_task

logger = logging.getLogger(__name__)

# Job states that mean "a worker has this, or is about to".
ACTIVE_JOB_STATUSES = (ProcessingJob.Status.QUEUED, ProcessingJob.Status.PROCESSING)

# A PROCESSING job whose row has not moved for this long is presumed dead.
# Set well above the Celery hard time limit plus Redis's one-hour
# visibility timeout: when a whole WORKER dies with a task in hand, acks_late
# leaves the message to be redelivered after that hour, and declaring the
# job dead sooner would race the redelivery that is about to finish it. A
# pool child killed by the hard limit or the OOM killer is NOT redelivered
# (Celery acknowledges it as failed - see CELERY_TASK_ACKS_LATE); this
# threshold and recover_stalled_jobs are what reclaim that job.
DEFAULT_STALLED_JOB_MINUTES = 120


class DocumentArchived(Exception):
    """The operation needs a live document, and this one is archived."""


class ProcessingInProgress(Exception):
    """A job for this version is already queued or running."""

    def __init__(self, job):
        super().__init__(
            f"Job {job.id} for this version is already {job.status.lower()}."
        )
        self.job = job


class DocumentQuarantined(Exception):
    """The security scan quarantined this version; it is not run again."""


def is_quarantined(version: DocumentVersion) -> bool:
    """Whether the scanner's last word on this version was "infected".

    Judged from the newest MALWARE_DETECTED job, not from the newest job.
    A fresh job starts with an empty error code, so reading the newest job
    meant a Reprocess click lifted the quarantine - every department member
    could download the file until the rescan caught up with it. Only a
    later run that COMPLETED (it passed the scan) clears the verdict.
    """
    flagged_at = (
        ProcessingJob.objects.filter(
            document_version=version, error_code=ErrorCode.MALWARE_DETECTED
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if flagged_at is None:
        return False
    return not ProcessingJob.objects.filter(
        document_version=version,
        status=ProcessingJob.Status.COMPLETED,
        created_at__gt=flagged_at,
    ).exists()


def stalled_cutoff():
    minutes = getattr(settings, "STALLED_JOB_MINUTES", DEFAULT_STALLED_JOB_MINUTES)
    return timezone.now() - timedelta(minutes=minutes)


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
    checksum = _compute_checksum(file_obj)

    with transaction.atomic():
        # The document row is locked before the next number is read. Reading
        # MAX(version_number) outside the lock - as this used to - let two
        # concurrent uploads of a new version both compute the same number;
        # the unique constraint then turned the second into an
        # IntegrityError and a 500, for a request that had done nothing
        # wrong. Serializing here makes concurrent uploads queue behind one
        # another and each get its own number.
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if locked.is_archived:
            raise DocumentArchived("Restore the document before adding a version.")

        next_version_number = (
            locked.versions.aggregate(models.Max("version_number"))[
                "version_number__max"
            ]
            or 0
        ) + 1

        version = DocumentVersion.objects.create(
            document=locked,
            version_number=next_version_number,
            file=file_obj,
            file_name=file_obj.name,
            file_size=file_obj.size,
            mime_type=getattr(file_obj, "content_type", "") or "",
            checksum=checksum,
            uploaded_by=uploaded_by,
        )

        locked.current_version = version
        locked.status = Document.Status.QUEUED
        locked.save(update_fields=["current_version", "status", "updated_at"])

        job = ProcessingJob.objects.create(document_version=version)

    # The caller's instance is brought up to date, so it can be serialized
    # straight back without another round trip.
    document.current_version = version
    document.status = locked.status

    transaction.on_commit(lambda: enqueue(job.id))

    return version


def enqueue(job_id: int) -> None:
    """Hand a committed job to the broker - or record that it could not be.

    Runs after the commit, so the document, version and QUEUED job already
    exist. A broker that is down used to leave exactly that: a job QUEUED
    with no message behind it, which counts as an active run (Reprocess
    refused with 409) and which stalled-job recovery never reclaims, plus a
    500 for the upload that HAD been stored. Recorded as a failed job
    instead, it says why and can be retried from the Processing screen.
    """
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        # Eager mode runs the pipeline INSIDE delay(); what it raises is the
        # pipeline's own outcome, not a broker that could not be reached.
        process_document_task.delay(job_id)
        return
    try:
        process_document_task.delay(job_id)
    except Exception as exc:
        logger.warning(
            "pipeline.enqueue_failed",
            extra={"job_id": job_id, "error": type(exc).__name__},
        )
        job = ProcessingJob.objects.select_related("document_version__document").get(
            pk=job_id
        )
        ProcessingJob.objects.filter(
            pk=job_id, status=ProcessingJob.Status.QUEUED
        ).update(
            status=ProcessingJob.Status.FAILED,
            error_code=ErrorCode.INFRASTRUCTURE_ERROR,
            error_message=f"Could not be queued for processing: {type(exc).__name__}",
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        document = job.document_version.document
        Document.objects.filter(
            pk=document.pk, current_version=job.document_version
        ).update(status=Document.Status.FAILED, updated_at=timezone.now())


def reprocess_document(document: Document) -> ProcessingJob:
    """Queue a new pipeline run over the document's current version.

    Refused while a run for that version is still queued or processing.
    Two runs over the same version race each other to be the live
    extraction pass, and whichever commits last silently wins - so a
    double-clicked Reprocess button was a data race, not a retry. A job
    that has stopped reporting progress for longer than STALLED_JOB_MINUTES
    is presumed dead and does not block: it is marked STALLED and replaced.
    """
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        version = locked.current_version

        if version is None:
            raise ValueError("Document has no current version to reprocess.")
        if locked.is_archived:
            raise DocumentArchived("Restore the document before reprocessing it.")
        # Never run again: the scan runs before any parser, so a rerun only
        # re-quarantines it, and meanwhile the document reads as QUEUED.
        # A file the scanner was wrong about is uploaded as a new version.
        if is_quarantined(version):
            raise DocumentQuarantined(
                "This file was quarantined by the security scan and is not "
                "processed again. Upload a new version instead."
            )

        active = (
            ProcessingJob.objects.select_for_update()
            .filter(document_version=version, status__in=ACTIVE_JOB_STATUSES)
            .order_by("-created_at")
        )
        for existing in active:
            if existing.updated_at >= stalled_cutoff():
                raise ProcessingInProgress(existing)
            _mark_stalled(existing)

        locked.status = Document.Status.QUEUED
        locked.save(update_fields=["status", "updated_at"])

        job = ProcessingJob.objects.create(document_version=version)

    document.status = locked.status
    transaction.on_commit(lambda: enqueue(job.id))

    return job


def archive_document(document: Document, user) -> Document:
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if not locked.is_archived:
            locked.archived_at = timezone.now()
            locked.archived_by = user if user and user.is_authenticated else None
            locked.save(update_fields=["archived_at", "archived_by", "updated_at"])
    return locked


def restore_document(document: Document) -> Document:
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if locked.is_archived:
            locked.archived_at = None
            locked.archived_by = None
            locked.save(update_fields=["archived_at", "archived_by", "updated_at"])
    return locked


def _mark_stalled(job: ProcessingJob) -> None:
    job.status = ProcessingJob.Status.FAILED
    job.error_code = ErrorCode.STALLED
    job.error_message = (
        f"No progress since {job.updated_at.isoformat()}; the worker running "
        f"this job was presumed lost."
    )
    job.completed_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "error_code",
            "error_message",
            "completed_at",
            "updated_at",
        ]
    )

    # Only the document whose CURRENT version this job was for changes
    # status. A stalled job for a superseded version says nothing about the
    # version people are now reading.
    Document.objects.filter(
        pk=job.document_version.document_id,
        current_version_id=job.document_version_id,
        status__in=(Document.Status.QUEUED, Document.Status.PROCESSING),
    ).update(status=Document.Status.FAILED, updated_at=timezone.now())


def recover_stalled_jobs(*, requeue: bool = False) -> list:
    """Mark every job that has stopped making progress as STALLED.

    A job is stalled when it is PROCESSING and its row has not been updated
    since the cutoff - every stage writes `current_stage`, so a live job
    touches the row at least once per stage. QUEUED jobs are deliberately
    left alone: a long backlog is not a failure, and a job waiting behind
    a bulk re-index is exactly as healthy as one that has started.

    With `requeue`, a fresh job is queued for each recovered job whose
    version is still the document's current one. Superseded versions are
    not requeued - their output would never be read.

    Returns (job, requeued_job_or_None) pairs.
    """
    recovered = []
    cutoff = stalled_cutoff()

    with transaction.atomic():
        stalled = list(
            ProcessingJob.objects.select_for_update(skip_locked=True)
            .select_related("document_version__document")
            .filter(status=ProcessingJob.Status.PROCESSING, updated_at__lt=cutoff)
        )
        for job in stalled:
            _mark_stalled(job)

    for job in stalled:
        replacement = None
        document = job.document_version.document
        document.refresh_from_db(fields=["current_version", "archived_at"])
        if (
            requeue
            and document.current_version_id == job.document_version_id
            and not document.is_archived
        ):
            try:
                replacement = reprocess_document(document)
            except (ProcessingInProgress, DocumentQuarantined):
                replacement = None
        recovered.append((job, replacement))

    return recovered


def retry_job(job: ProcessingJob) -> ProcessingJob:
    """Queue a fresh run for a finished job's version, if it is still current.

    Retrying a superseded version's job would produce an extraction pass
    nothing reads, so it is refused rather than quietly done.
    """
    document = job.document_version.document
    if document.current_version_id != job.document_version_id:
        raise ValueError(
            "This job is for a superseded version; reprocess the current "
            "version instead."
        )
    return reprocess_document(document)
