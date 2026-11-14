import time

from django.conf import settings

from documents.models import PageContent
from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import ocr as ocr_extractor

# The resolutions a PDF page is rendered at, best first. Cost falls roughly
# with the square of the resolution.
DPI_STEPS = (300, 200, 150, 100)


def _lower(dpi: int) -> int:
    """The next resolution down, or `dpi` itself at the bottom."""
    below = [step for step in DPI_STEPS if step < dpi]
    return below[0] if below else dpi


def run(context) -> StageResult:
    pending = context.pages_needing_ocr()

    if not pending:
        _require_some_text(context)
        return StageResult(
            message="No pages required OCR.",
            metadata={"pages_ocred": 0},
        )

    started = time.monotonic()
    dpi = settings.OCR_DPI
    budget = settings.OCR_TIME_BUDGET_SECONDS
    # Measured at the resolution in use, so a step down is judged on what
    # the new resolution costs and not on the one it replaced.
    spent_at_dpi = 0.0
    pages_at_dpi = 0
    reduced_from = None

    for position, page in enumerate(pending):
        page_started = time.monotonic()
        if context.file_type == FileType.IMAGE:
            # One page per frame: a multi-page TIFF is a scanned document.
            result = ocr_extractor.ocr_image_file(
                context.file_path, frame=page.page_number - 1
            )
        else:
            result = ocr_extractor.ocr_pdf_page(
                context.file_path, page.page_number, dpi
            )

        # Why the text layer was passed over (unmapped glyphs, none at all),
        # if the extractor said - OCR text is a different thing to trust.
        reason = page.metadata.get("ocr_reason")

        page.text = result.text
        page.tables = result.tables
        page.extraction_method = PageContent.ExtractionMethod.OCR
        page.metadata = {
            "extractor": "tesseract",
            "extractor_version": ocr_extractor.tesseract_version(),
            "dpi": dpi if context.file_type == FileType.PDF else None,
            **result.metadata,
            **({"ocr_reason": reason} if reason else {}),
        }

        # If the pages still to read, at the pace this resolution has kept,
        # will not fit in what is left, read them at the next one down.
        # Quality is spent before the document is: a lower resolution
        # reads a little worse, a timeout reads nothing.
        spent_at_dpi += time.monotonic() - page_started
        pages_at_dpi += 1
        remaining = len(pending) - position - 1
        elapsed = time.monotonic() - started
        pace = spent_at_dpi / pages_at_dpi
        if (
            context.file_type == FileType.PDF
            and remaining
            and elapsed + remaining * pace > budget
            and _lower(dpi) != dpi
        ):
            reduced_from = reduced_from or dpi
            dpi = _lower(dpi)
            spent_at_dpi = 0.0
            pages_at_dpi = 0

    duration = time.monotonic() - started
    _require_some_text(context)

    message = f"OCR ran on {len(pending)} page(s)."
    if reduced_from:
        message += (
            f" Resolution was lowered from {reduced_from} to {dpi} DPI part-way "
            f"through, to finish within {budget} seconds."
        )

    return StageResult(
        message=message,
        metadata={
            "pages_ocred": len(pending),
            **(
                {"dpi_lowered_from": reduced_from, "dpi_final": dpi}
                if reduced_from
                else {}
            ),
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
