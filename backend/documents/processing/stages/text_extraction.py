from documents.models import PageContent
from documents.processing.context import ExtractedPage, FileType, StageResult
from documents.processing.extractors import pdf, plain_text


def run(context) -> StageResult:
    """Fill in whatever text is available without OCR.

    Pages left with `extraction_method = None` carry no text layer and
    are picked up by the OCR stage - keeping the two paths separately
    measurable rather than hidden behind one fallback.
    """
    if context.file_type == FileType.TXT:
        return _extract_plain_text(context)

    if context.file_type == FileType.PDF:
        return _extract_pdf_text(context)

    return _defer_image_to_ocr(context)


def _extract_plain_text(context) -> StageResult:
    text = plain_text.read_text(context.file_path)

    context.pages = [
        ExtractedPage(
            page_number=1,
            text=text,
            extraction_method=PageContent.ExtractionMethod.PLAIN_TEXT,
            metadata={"extractor": "builtin", "extractor_version": ""},
        )
    ]

    return StageResult(
        message=f"Read {len(text)} characters from plain text.",
        metadata={"pages": 1, "characters": len(text)},
    )


def _extract_pdf_text(context) -> StageResult:
    page_texts = pdf.extract_pages(context.file_path)

    for index, text in enumerate(page_texts, start=1):
        has_text = bool(text.strip())
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=text if has_text else "",
                extraction_method=(
                    PageContent.ExtractionMethod.PDF_TEXT if has_text else None
                ),
                metadata=(
                    {"extractor": "pypdf", "extractor_version": pdf.VERSION}
                    if has_text
                    else {}
                ),
            )
        )

    with_text = sum(1 for page in context.pages if page.has_text)
    needs_ocr = len(context.pages) - with_text

    return StageResult(
        message=(
            f"{with_text}/{len(context.pages)} pages had a text layer; "
            f"{needs_ocr} deferred to OCR."
        ),
        metadata={
            "pages": len(context.pages),
            "pages_with_text_layer": with_text,
            "pages_needing_ocr": needs_ocr,
            "characters": sum(len(page.text) for page in context.pages),
        },
    )


def _defer_image_to_ocr(context) -> StageResult:
    context.pages = [ExtractedPage(page_number=1)]

    return StageResult(
        message="Image has no text layer; deferred to OCR.",
        metadata={"pages": 1, "pages_needing_ocr": 1},
    )
