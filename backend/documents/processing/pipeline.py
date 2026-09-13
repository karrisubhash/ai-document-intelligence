import time

from django.db import transaction
from django.utils import timezone

from documents.models import (
    ClassificationResult,
    Document,
    DocumentContent,
    DocumentFigure,
    DocumentTable,
    ExtractionResult,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)

from documents import storage
from documents.observability import telemetry

from .context import IngestionContext, StageResult
from .errors import ErrorCode
from .normalize import strip_nul
from .figures import detector as figure_detector
from .tables import detector as layout_detector
from .scanning import DocumentInfected
from .stages import (
    chunking,
    classification,
    field_extraction,
    ingestion,
    layout_analysis,
    ocr,
    scanning,
    text_extraction,
    validation,
)

# Bumped whenever the pipeline changes in a way that alters its output,
# so stored content can be traced back to what produced it.
PROCESSING_VERSION = "ingestion-v1"

# Stages that build the normalized representation from the raw file.
CONTENT_STAGES = [
    (ProcessingJob.Stage.INGESTION, ingestion.run),
    # Scanning comes BEFORE validation, and the order is deliberate.
    # Validation answers a usability question - "is this a document type we
    # support?" - while scanning answers a security one: "is this hostile?"
    # Running validation first meant an executable renamed .pdf was rejected
    # as UNSUPPORTED_FILE_TYPE and never scanned, so the one fact worth
    # escalating - that somebody uploaded a binary - was the fact thrown
    # away. INGESTION has already bounded the file size, so nothing
    # unbounded reaches the scanner.
    (ProcessingJob.Stage.SCANNING, scanning.run),
    (ProcessingJob.Stage.VALIDATION, validation.run),
    (ProcessingJob.Stage.TEXT_EXTRACTION, text_extraction.run),
    (ProcessingJob.Stage.OCR, ocr.run),
    # Last in the content phase, because it reads the finished page text -
    # a table that only exists on a scanned page is not there to be found
    # until OCR has filled that page in.
    (ProcessingJob.Stage.LAYOUT_ANALYSIS, layout_analysis.run),
]

# Stages that consume the persisted DocumentContent - each reading the same
# internal representation rather than reopening the original file.
ANALYSIS_STAGES = [
    (ProcessingJob.Stage.CLASSIFICATION, classification.run),
    (ProcessingJob.Stage.EXTRACTION, field_extraction.run),
    (ProcessingJob.Stage.INDEXING, chunking.run),
]


def run_pipeline(processing_job_id: int) -> None:
    job = ProcessingJob.objects.select_related(
        "document_version__document"
    ).get(id=processing_job_id)
    document_version = job.document_version
    document = document_version.document

    _mark_started(job, document)

    # The file is materialized ONCE, for the whole job, and every stage
    # shares that path. Reading `document_version.file.path` directly - as
    # this used to - works only on a filesystem storage: Storage.path()
    # raises NotImplementedError on S3 and every other remote backend, so
    # the platform could not be pointed at object storage at all, however
    # it was configured. See documents/storage.py.
    #
    # Held open across the analysis stages too, even though they read the
    # persisted DocumentContent rather than the file, so that a retry of a
    # single stage never has to fetch the object a second time.
    try:
        with storage.local_copy(document_version.file) as file_path:
            context = IngestionContext(
                document_version=document_version,
                file_path=file_path,
                processing_job=job,
            )

            for stage, stage_fn in CONTENT_STAGES:
                _run_stage(job, context, stage, stage_fn)

            # The normalized representation lands here, before anything
            # analyses it: File -> Ingestion -> Validation ->
            # DocumentContent -> Classification.
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

        telemetry.stage_failed(
            stage,
            getattr(exc, "code", ErrorCode.UNEXPECTED_ERROR),
            getattr(exc, "retryable", False),
            document_id=job.document_version.document_id,
            job_id=job.id,
        )
        telemetry.stage_finished(
            stage, event.status, time.monotonic() - started,
            document_id=job.document_version.document_id, job_id=job.id,
        )
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

    telemetry.stage_finished(
        stage,
        event.status,
        time.monotonic() - started,
        document_id=job.document_version.document_id,
        job_id=job.id,
    )


def _persist_content(job, context) -> DocumentContent:
    """Store the page-level representation against the exact version.

    Replacement happens here, in one transaction, rather than up front at
    the start of the attempt. Deleting eagerly would mean a retry that
    fails early - a storage blip, a provider outage - destroys the good
    content the previous attempt produced and puts nothing in its place,
    trading duplicate data for missing data. Doing it here means the old
    rows survive until the moment there is something to replace them with.
    """
    document_version = context.document_version

    with transaction.atomic():
        # Serialize replacement against a concurrent attempt at the same
        # job. A broker can deliver the same task twice; without this, two
        # workers could each delete and then each insert, recreating the
        # duplication this is here to prevent. The lock is taken here
        # rather than around the whole pipeline so it is never held across
        # OCR.
        ProcessingJob.objects.select_for_update().get(id=job.id)

        _discard_previous_attempt(job)

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
                    text=strip_nul(page.text),
                    extraction_method=page.extraction_method,
                    character_count=len(page.text),
                    metadata=page.metadata,
                )
                for page in context.pages
            ]
        )

        # Same transaction as the pages, deliberately. A table stores
        # offsets into page text; committing it separately would allow a
        # window in which a table points into a page that is not there
        # yet, or survives a page that has been replaced.
        DocumentTable.objects.bulk_create(
            [
                DocumentTable(
                    document_version=document_version,
                    document_content=content,
                    processing_job=job,
                    table_index=table.table_index,
                    page_number=table.page_number,
                    character_start=table.character_start,
                    character_end=table.character_end,
                    rows=table.rows,
                    row_count=table.row_count,
                    column_count=table.column_count,
                    header=table.header,
                    detector=layout_detector.DETECTOR_NAME,
                    detector_version=layout_detector.DETECTOR_VERSION,
                    metadata={"separator": table.separator},
                )
                for table in context.tables
            ]
        )

        DocumentFigure.objects.bulk_create(
            [
                DocumentFigure(
                    document_version=document_version,
                    document_content=content,
                    processing_job=job,
                    page_number=figure.page_number,
                    figure_index=figure.figure_index,
                    width=figure.width,
                    height=figure.height,
                    description=figure.description,
                    description_source=figure.description_source,
                    detector=figure_detector.DETECTOR_NAME,
                    detector_version=figure_detector.DETECTOR_VERSION,
                    metadata=figure.metadata,
                )
                for figure in context.figures
            ]
        )

        # This pass becomes the live one for the version, in the same
        # transaction that wrote it. Retrieval filters on this pointer, so
        # a half-written pass must never be reachable: until this line
        # commits, the previous content is still the one search uses.
        document_version.current_content = content

        # Kept as a convenience view of the same content - the page-level
        # rows above are the representation everything downstream uses.
        #
        # strip_nul here too, not only on the page rows. The first version
        # stripped the pages and then wrote the raw text straight into this
        # column, so a document containing a NUL byte still processed every
        # stage successfully and then died on the very last write of the
        # transaction - the exact failure the stripping was added to
        # prevent, moved down by three lines. Found by a test that fed a
        # NUL through the real pipeline rather than through the helper.
        document_version.extracted_text = strip_nul(
            "\n".join(page.text for page in context.pages)
        ).strip()
        document_version.save(update_fields=["extracted_text", "current_content"])

    counts = {}
    for page in context.pages:
        counts[page.extraction_method] = counts.get(page.extraction_method, 0) + 1
    telemetry.pages_extracted(counts)

    return content


def _discard_previous_attempt(job: ProcessingJob) -> None:
    """Remove whatever an earlier attempt at THIS job already wrote.

    Tasks are retried, so run_pipeline can legitimately execute more than
    once for the same job - and every stage here creates rather than
    updates. Without this, a transient failure in a late stage leaves a
    complete copy of the document's content behind and the next attempt
    appends a second one. Section 10.2 requires the opposite: "a repeated
    message does not create duplicate chunks, duplicate embeddings, or
    inconsistent metadata".

    MUST be called inside the transaction that writes the replacement -
    see _persist_content. Called on its own it is a destructive operation
    with no guarantee that anything takes the old rows' place.

    Scoped to this job on purpose. History *across* jobs is untouched - a
    reprocess creates a new ProcessingJob, so its output sits beside the
    old rather than replacing it, which is what makes two pipeline
    versions comparable. ProcessingEvent rows are also left alone: they
    are the attempt history, and losing the record of a failed attempt
    would be the dishonest kind of cleanup.
    """
    ClassificationResult.objects.filter(processing_job=job).delete()
    ExtractionResult.objects.filter(processing_job=job).delete()
    # Cascades to PageContent, DocumentTable and the chunks and
    # embeddings derived from them - everything that was keyed to this
    # extraction pass goes with it, which is why it has to be replaced
    # in the same transaction.
    DocumentContent.objects.filter(processing_job=job).delete()


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

    telemetry.job_finished(
        document.status, document_id=document.id, job_id=job.id, attempt=job.attempt
    )


def _mark_failed(job: ProcessingJob, document: Document, exc: Exception) -> None:
    quarantined = isinstance(exc, DocumentInfected)

    job.status = ProcessingJob.Status.FAILED
    job.error_code = (
        ErrorCode.MALWARE_DETECTED
        if quarantined
        else getattr(exc, "code", ErrorCode.UNEXPECTED_ERROR)
    )
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

    # A quarantined document is not a failed one. FAILED invites a retry;
    # QUARANTINED is a decision not to touch the file, and collapsing the
    # two would lose exactly the distinction an operator needs (spec 10.1).
    document.status = (
        Document.Status.QUARANTINED if quarantined else Document.Status.FAILED
    )
    document.save(update_fields=["status", "updated_at"])

    telemetry.job_finished(
        document.status, document_id=document.id, job_id=job.id, attempt=job.attempt
    )
