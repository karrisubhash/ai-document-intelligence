import logging

from celery import shared_task
from django.conf import settings
from django.db import InterfaceError, OperationalError

from . import storage
from .models import ProcessingJob
from .processing import limits
from .processing.errors import StageError, ran_out_of_time
from .processing.pipeline import run_pipeline
from .processing.scanning import DocumentInfected

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# Only genuine infrastructure faults earn another attempt. Anything else -
# a bad document or a bug in our own code - would fail identically on every
# retry, so it fails once and stays failed.
RETRYABLE_EXCEPTIONS = (
    OperationalError,  # dropped/refused database connection
    InterfaceError,
    ConnectionError,
    TimeoutError,
)


def is_retryable(exc: Exception) -> bool:
    # A document that ran out of time will run out of time again, however
    # the timeout was wrapped on its way out (see ran_out_of_time).
    if ran_out_of_time(exc):
        return False
    # A quarantined file does not become clean on the next attempt.
    if isinstance(exc, DocumentInfected):
        return False
    if isinstance(exc, StageError):
        return exc.retryable
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def requeue_with_longer_limits(task, processing_job_id: int) -> bool:
    """Send this job again with time limits that fit its length; True if sent.

    Celery fixes a task's time limits when the message is sent, and the
    page count of a PDF is not known then (reading it costs seconds on a long
    file - too long to spend inside an upload request). So a task starts with
    the base limits, reads the page count, and if the document needs more
    sends itself once more with limits to match, before doing any work. The
    message that arrives carries the larger limits, so it never does this
    twice; neither does a retry, which keeps its message's limits.

    Never raises: any failure here leaves the job to run under the base
    limits, which is what it would have had without this.
    """
    request = task.request
    if request.called_directly or request.is_eager:
        return False

    try:
        job = ProcessingJob.objects.select_related("document_version").get(
            id=processing_job_id
        )
        with storage.local_copy(job.document_version.file) as path:
            pages = limits.pdf_page_count(path)

        soft = limits.soft_limit_for(pages)
        _, current_soft = request.timelimit or (None, None)
        if soft <= (current_soft or settings.CELERY_TASK_SOFT_TIME_LIMIT):
            return False

        task.apply_async(
            (processing_job_id,),
            soft_time_limit=soft,
            time_limit=limits.hard_limit_for(soft),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pipeline.limits_not_extended",
            extra={"job_id": processing_job_id, "error": type(exc).__name__},
        )
        return False

    logger.info(
        "pipeline.limits_extended",
        extra={"job_id": processing_job_id, "pages": pages, "soft_seconds": soft},
    )
    return True


@shared_task(bind=True, max_retries=MAX_RETRIES)
def process_document_task(self, processing_job_id: int):
    if requeue_with_longer_limits(self, processing_job_id):
        return

    def will_retry(exc):
        return is_retryable(exc) and self.request.retries < self.max_retries

    try:
        run_pipeline(processing_job_id, will_retry=will_retry)
    except Exception as exc:
        if is_retryable(exc):
            raise self.retry(exc=exc, countdown=2**self.request.retries) from exc
        raise
