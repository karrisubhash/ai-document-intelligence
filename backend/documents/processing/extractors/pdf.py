import pypdf
from pypdf import PdfReader

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
    return len(open_reader(file_path).pages)


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
