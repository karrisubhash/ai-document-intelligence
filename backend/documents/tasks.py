from celery import shared_task
from django.db import InterfaceError, OperationalError

from .processing.errors import StageError
from .processing.pipeline import run_pipeline

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
    if isinstance(exc, StageError):
        return exc.retryable
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


@shared_task(bind=True, max_retries=MAX_RETRIES)
def process_document_task(self, processing_job_id: int):
    try:
        run_pipeline(processing_job_id)
    except Exception as exc:
        if is_retryable(exc):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise
