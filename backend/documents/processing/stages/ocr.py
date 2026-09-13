import time

from django.conf import settings

from documents.models import PageContent
from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import ocr as ocr_extractor


def run(context) -> StageResult:
    pending = context.pages_needing_ocr()

    if not pending:
        _require_some_text(context)
        return StageResult(
            message="No pages required OCR.",
            metadata={"pages_ocred": 0},
        )

    started = time.monotonic()

    for page in pending:
        if context.file_type == FileType.IMAGE:
            text = ocr_extractor.ocr_image_file(context.file_path)
        else:
            text = ocr_extractor.ocr_pdf_page(
                context.file_path, page.page_number, settings.OCR_DPI
            )

        page.text = text
        page.extraction_method = PageContent.ExtractionMethod.OCR
        page.metadata = {
            "extractor": "tesseract",
            "extractor_version": ocr_extractor.tesseract_version(),
            "dpi": settings.OCR_DPI if context.file_type == FileType.PDF else None,
        }

    duration = time.monotonic() - started
    _require_some_text(context)

    return StageResult(
        message=f"OCR ran on {len(pending)} page(s).",
        metadata={
            "pages_ocred": len(pending),
            # Named distinctly because the pipeline stamps its own
            # "duration_seconds" for the whole stage over the top of this
            # dict. This one times only the OCR loop, which is the number
            # worth comparing across documents.
            "ocr_seconds": round(duration, 3),
            "characters": sum(len(page.text) for page in pending),
        },
    )


def _require_some_text(context) -> None:
    if not any(page.has_text for page in context.pages):
        raise StageError(
            "No text could be extracted from the document.",
            code=ErrorCode.EMPTY_EXTRACTION,
        )
