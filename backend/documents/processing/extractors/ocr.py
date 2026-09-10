import functools

import pytesseract
from pdf2image import convert_from_path
from PIL import Image

from documents.processing.errors import ErrorCode, StageError


@functools.cache
def tesseract_version() -> str:
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "unknown"


def ocr_image_file(file_path: str) -> str:
    try:
        with Image.open(file_path) as image:
            return pytesseract.image_to_string(image)
    except Exception as exc:
        raise StageError(
            f"OCR failed for image: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc


def ocr_pdf_page(file_path: str, page_number: int, dpi: int) -> str:
    """Render a single PDF page and OCR it.

    Rendered one page at a time on purpose - converting a whole scanned
    document at once is what makes OCR memory usage blow up.
    """
    try:
        images = convert_from_path(
            file_path,
            dpi=dpi,
            first_page=page_number,
            last_page=page_number,
        )
    except Exception as exc:
        raise StageError(
            f"Could not render page {page_number} for OCR: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc

    if not images:
        raise StageError(
            f"No rendered image produced for page {page_number}.",
            code=ErrorCode.OCR_FAILED,
        )

    try:
        with images[0] as image:
            return pytesseract.image_to_string(image)
    except Exception as exc:
        raise StageError(
            f"OCR failed for page {page_number}: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc
