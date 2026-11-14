from documents.models import PageContent
from documents.processing.context import ExtractedPage, FileType, StageResult
from documents.processing.extractors import docx, pdf, plain_text
from documents.processing.normalize import strip_nul


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

    if context.file_type == FileType.DOCX:
        return _extract_docx_text(context)

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
    results = pdf.extract_page_results(context.file_path)

    for index, result in enumerate(results, start=1):
        # Decided on the text that will be stored: a text layer of nothing
        # but NUL bytes is no text layer, and the page belongs to OCR. So is
        # one the extractor judged unreadable (unmapped glyphs).
        has_text = not result.needs_ocr and bool(strip_nul(result.text).strip())
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=result.text if has_text else "",
                extraction_method=(
                    PageContent.ExtractionMethod.PDF_TEXT if has_text else None
                ),
                metadata=(
                    {
                        "extractor": result.extractor,
                        "extractor_version": result.extractor_version,
                        **result.metadata,
                    }
                    if has_text
                    # Kept for the OCR stage, which records why it was asked.
                    else {
                        key: value
                        for key, value in result.metadata.items()
                        if key == "ocr_reason"
                    }
                ),
                tables=result.tables if has_text else [],
            )
        )

    with_text = sum(1 for page in context.pages if page.has_text)
    needs_ocr = len(context.pages) - with_text
    fell_back = sum(1 for result in results if result.extractor == "pypdf")

    metadata = {
        "pages": len(context.pages),
        "pages_with_text_layer": with_text,
        "pages_needing_ocr": needs_ocr,
        "characters": sum(len(page.text) for page in context.pages),
        "pdf_extractor": pdf.EXTRACTOR,
        "pdf_extractor_version": pdf.VERSION,
    }
    if fell_back:
        metadata["pages_read_by_fallback"] = fell_back

    message = (
        f"{with_text}/{len(context.pages)} pages had a text layer; "
        f"{needs_ocr} deferred to OCR."
    )
    if fell_back:
        message += f" {fell_back} read by the fallback extractor."
    return StageResult(message=message, metadata=metadata)


def _extract_docx_text(context) -> StageResult:
    """DOCX carries its own text, so it never reaches OCR.

    A DOCX whose content is entirely embedded images yields nothing here
    and fails the OCR stage's "some text must exist" check, which is the
    honest outcome: rendering and OCR-ing embedded images is a different
    capability this pipeline does not claim to have.
    """
    page_texts, metadata = docx.extract_pages(context.file_path)

    for index, text in enumerate(page_texts, start=1):
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=text,
                extraction_method=PageContent.ExtractionMethod.DOCX_TEXT,
                # A fresh dict per page, holding only what is true of that
                # page. Sharing one object across pages would alias them,
                # and copying document-level counts onto every row would
                # state them as if they were per-page facts.
                metadata={
                    "extractor": metadata["extractor"],
                    "extractor_version": metadata["extractor_version"],
                    "pagination": metadata["pagination"],
                },
            )
        )

    # Document-level facts belong on DocumentContent, not on each page.
    context.metadata.update(
        {
            key: value
            for key, value in metadata.items()
            if key not in ("extractor", "extractor_version")
        }
    )

    characters = sum(len(page.text) for page in context.pages)
    return StageResult(
        message=(
            f"Read {characters} characters from {len(page_texts)} "
            f"page(s) ({metadata['paragraphs']} paragraphs, "
            f"{metadata['tables']} table(s))."
        ),
        metadata={
            "pages": len(page_texts),
            "characters": characters,
            **metadata,
        },
    )


def _defer_image_to_ocr(context) -> StageResult:
    context.pages = [ExtractedPage(page_number=1)]

    return StageResult(
        message="Image has no text layer; deferred to OCR.",
        metadata={"pages": 1, "pages_needing_ocr": 1},
    )
