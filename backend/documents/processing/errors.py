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
