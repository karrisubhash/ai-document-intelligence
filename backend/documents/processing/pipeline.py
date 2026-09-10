import time

from django.db import transaction
from django.utils import timezone

from documents.models import (
    Document,
    DocumentContent,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)

from .context import IngestionContext, StageResult
from .errors import ErrorCode
from .stages import classification, ingestion, ocr, text_extraction, validation

# Bumped whenever the pipeline changes in a way that alters its output,
# so stored content can be traced back to what produced it.
PROCESSING_VERSION = "ingestion-v1"

# Stages that build the normalized representation from the raw file.
CONTENT_STAGES = [
    (ProcessingJob.Stage.INGESTION, ingestion.run),
    (ProcessingJob.Stage.VALIDATION, validation.run),
    (ProcessingJob.Stage.TEXT_EXTRACTION, text_extraction.run),
    (ProcessingJob.Stage.OCR, ocr.run),
]

# Stages that consume the persisted DocumentContent. LAYOUT_ANALYSIS,
# ENTITY_EXTRACTION, TABLE_EXTRACTION and INDEXING join here once they
# are benchmarked - each reading the same internal representation rather
# than reopening the original file.
ANALYSIS_STAGES = [
    (ProcessingJob.Stage.CLASSIFICATION, classification.run),
]


def run_pipeline(processing_job_id: int) -> None:
    job = ProcessingJob.objects.select_related(
        "document_version__document"
    ).get(id=processing_job_id)
    document_version = job.document_version
    document = document_version.document

    context = IngestionContext(
        document_version=document_version,
        file_path=document_version.file.path,
        processing_job=job,
    )

    _mark_started(job, document)

    try:
        for stage, stage_fn in CONTENT_STAGES:
            _run_stage(job, context, stage, stage_fn)

        # The normalized representation lands here, before anything
        # analyses it: File -> Ingestion -> Validation -> DocumentContent
        # -> Classification.
        context.content = _persist_content(job, context)

        for stage, stage_fn in ANALYSIS_STAGES:
            _run_stage(job, context, stage, stage_fn)
    except Exception as exc:
        _mark_failed(job, document, exc)
        raise
    else:
        _mark_completed(job, document)


def _run_stage(job, context, stage, stage_fn) -> None:
    job.current_stage = stage
    job.save(update_fields=["current_stage", "updated_at"])

    event = ProcessingEvent.objects.create(
        processing_job=job,
        stage=stage,
        status=ProcessingEvent.Status.STARTED,
        started_at=timezone.now(),
    )
    started = time.monotonic()

    try:
        result = stage_fn(context)
    except Exception as exc:
        event.status = ProcessingEvent.Status.FAILED
        event.message = str(exc)
        event.metadata = {
            "error_code": getattr(exc, "code", ErrorCode.UNEXPECTED_ERROR),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        event.completed_at = timezone.now()
        event.save(update_fields=["status", "message", "metadata", "completed_at"])
        raise

    if result is None:
        result = StageResult()

    event.status = (
        ProcessingEvent.Status.SKIPPED
        if result.skipped
        else ProcessingEvent.Status.SUCCEEDED
    )
    event.message = result.message
    event.metadata = {
        **result.metadata,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    event.completed_at = timezone.now()
    event.save(update_fields=["status", "message", "metadata", "completed_at"])


def _persist_content(job, context) -> DocumentContent:
    """Store the page-level representation against the exact version."""
    document_version = context.document_version

    with transaction.atomic():
        content = DocumentContent.objects.create(
            document_version=document_version,
            processing_job=job,
            processing_version=PROCESSING_VERSION,
            page_count=len(context.pages),
            metadata={
                "file_type": context.file_type,
                **context.metadata,
            },
        )

        PageContent.objects.bulk_create(
            [
                PageContent(
                    content=content,
                    page_number=page.page_number,
                    text=page.text,
                    extraction_method=page.extraction_method,
                    character_count=len(page.text),
                    metadata=page.metadata,
                )
                for page in context.pages
            ]
        )

        # Kept as a convenience view of the same content - the page-level
        # rows above are the representation everything downstream uses.
        document_version.extracted_text = "\n".join(
            page.text for page in context.pages
        ).strip()
        document_version.save(update_fields=["extracted_text"])

    return content


def _mark_started(job: ProcessingJob, document: Document) -> None:
    job.status = ProcessingJob.Status.PROCESSING
    job.attempt += 1
    job.started_at = timezone.now()
    job.error_code = ""
    job.error_message = ""
    job.save(
        update_fields=[
            "status",
            "attempt",
            "started_at",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )

    document.status = Document.Status.PROCESSING
    document.save(update_fields=["status", "updated_at"])


def _mark_completed(job: ProcessingJob, document: Document) -> None:
    job.status = ProcessingJob.Status.COMPLETED
    job.completed_at = timezone.now()
    job.save(update_fields=["status", "completed_at", "updated_at"])

    document.status = Document.Status.COMPLETED
    document.save(update_fields=["status", "updated_at"])


def _mark_failed(job: ProcessingJob, document: Document, exc: Exception) -> None:
    job.status = ProcessingJob.Status.FAILED
    job.error_code = getattr(exc, "code", ErrorCode.UNEXPECTED_ERROR)
    job.error_message = str(exc)
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

    document.status = Document.Status.FAILED
    document.save(update_fields=["status", "updated_at"])
