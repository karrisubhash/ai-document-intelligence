class ErrorCode:
    """Structured failure codes recorded on ProcessingJob.error_code."""

    MISSING_FILE = "MISSING_FILE"
    EMPTY_FILE = "EMPTY_FILE"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    CORRUPT_DOCUMENT = "CORRUPT_DOCUMENT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    OCR_FAILED = "OCR_FAILED"
    EMPTY_EXTRACTION = "EMPTY_EXTRACTION"
    CLASSIFICATION_FAILED = "CLASSIFICATION_FAILED"
    # Distinct from EXTRACTION_FAILED above, which means TEXT extraction.
    FIELD_EXTRACTION_FAILED = "FIELD_EXTRACTION_FAILED"
    CHUNKING_FAILED = "CHUNKING_FAILED"
    MALWARE_DETECTED = "MALWARE_DETECTED"
    SCAN_FAILED = "SCAN_FAILED"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    # The job ran past its Celery soft time limit. Permanent: a document
    # that took longer than the limit once will take as long again.
    TIMEOUT = "TIMEOUT"
    # The job stopped reporting progress - the worker was killed, or hit
    # its hard time limit, and nothing marked the job finished. Set by
    # `manage.py recover_stalled_jobs`, never by the pipeline itself.
    STALLED = "STALLED"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class StageError(Exception):
    """A known, classifiable failure in a pipeline stage.

    Permanent by default: a document that is the wrong type, too large,
    corrupt or blank will fail identically on every attempt, so retrying
    it only burns worker capacity and writes a dishonest attempt history.
    Only failures that could plausibly succeed on a later attempt are
    marked retryable - see TransientError.
    """

    retryable = False

    def __init__(self, message, code=ErrorCode.UNEXPECTED_ERROR):
        super().__init__(message)
        self.code = code


class TransientError(StageError):
    """An infrastructure hiccup - the same input may well succeed later.

    For temporary storage/service unavailability and dropped database
    connections, not for anything about the document itself.
    """

    retryable = True

    def __init__(self, message, code=ErrorCode.INFRASTRUCTURE_ERROR):
        super().__init__(message, code=code)


def ran_out_of_time(exc: BaseException) -> bool:
    """Whether Celery's soft time limit is anywhere in this exception's chain.

    The limit is raised INSIDE whatever code is running when it passes, and
    much of that code wraps what it catches: the embedder turns any failure
    into EmbeddingProviderUnavailable (and so a retryable TransientError),
    the OCR extractors into StageError(OCR_FAILED). Judged by the outermost
    exception alone, a document that ran out of time was retried three more
    times - each with a fresh fifteen-minute budget, each running out at the
    same place - or recorded as an OCR failure. The cause chain still holds
    the truth; this reads it.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, SoftTimeLimitExceeded):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False
