import pypdf
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

from documents.processing.errors import ErrorCode, StageError

VERSION = pypdf.__version__


def open_reader(file_path: str) -> PdfReader:
    try:
        return PdfReader(file_path)
    except Exception as exc:
        raise StageError(
            f"Could not read PDF: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def page_count(file_path: str) -> int:
    reader = open_reader(file_path)

    # pypdf reads the page tree on first use, not when the reader is built,
    # so a password-protected PDF or a broken /Pages entry fails HERE - and
    # unwrapped, pypdf's own exception reached the job as UNEXPECTED_ERROR,
    # a bug report for what is a property of the file.
    try:
        return len(reader.pages)
    except FileNotDecryptedError as exc:
        raise StageError(
            "PDF is password-protected; upload a copy without a password.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc
    except Exception as exc:
        raise StageError(
            f"Could not read PDF: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def extract_pages(file_path: str) -> list[str]:
    """Text layer per page, index 0 = page 1. Blank where there is none."""
    reader = open_reader(file_path)

    try:
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise StageError(
            f"PDF text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc
