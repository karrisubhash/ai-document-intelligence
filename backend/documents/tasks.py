from celery import shared_task
from django.db import InterfaceError, OperationalError

from .processing.errors import StageError, ran_out_of_time
from .processing.pipeline import run_pipeline
from .processing.scanning import DocumentInfected

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


@shared_task(bind=True, max_retries=MAX_RETRIES)
def process_document_task(self, processing_job_id: int):
    def will_retry(exc):
        return is_retryable(exc) and self.request.retries < self.max_retries

    try:
        run_pipeline(processing_job_id, will_retry=will_retry)
    except Exception as exc:
        if is_retryable(exc):
            raise self.retry(exc=exc, countdown=2**self.request.retries) from exc
        raise
