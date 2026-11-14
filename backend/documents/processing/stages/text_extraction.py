import os

from django.conf import settings

from documents.models import PageContent
from documents.processing.context import ExtractedPage, FileType, StageResult
from documents.processing.extractors import (
    docx,
    pdf,
    plain_text,
    presentation,
    spreadsheet,
)
from documents.processing.extractors import ocr as ocr_extractor
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

    if context.file_type == FileType.XLSX:
        return _extract_sheets(context, *spreadsheet.extract_xlsx(context.file_path))

    if context.file_type == FileType.CSV:
        # A .tsv is tab-separated unless its content clearly says otherwise.
        name = context.document_version.file_name
        preferred = "\t" if os.path.splitext(name)[1].lower() == ".tsv" else ","
        return _extract_sheets(
            context, *spreadsheet.extract_delimited(context.file_path, preferred)
        )

    if context.file_type == FileType.PPTX:
        return _extract_slides(context)

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
    pictures = settings.OCR_EMBEDDED_IMAGES and settings.OCR_EMBEDDED_IMAGES_MAX > 0
    results = pdf.extract_page_results(
        context.file_path,
        # Pictures inside a page that has a text layer: a screenshot of a
        # table, a pasted scan. The page's own words and the picture's are
        # read together, so a table in a picture is a table.
        region_ocr=ocr_extractor.read_words if pictures else None,
        max_regions=settings.OCR_EMBEDDED_IMAGES_MAX if pictures else 0,
        dpi=settings.OCR_DPI,
    )

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

    Pictures embedded in the body are OCR'd when they look like documents
    (see extractors/pictures.py), up to a per-document budget. A DOCX with no
    text and nothing legible in its pictures still fails the OCR stage's
    "some text must exist" check, which is the honest outcome.
    """
    pictures = settings.OCR_EMBEDDED_IMAGES and settings.OCR_EMBEDDED_IMAGES_MAX > 0
    pages, metadata = docx.extract_page_results(
        context.file_path,
        # A scanned form pasted into a Word file is the content; reading it
        # is what stops such a document failing as empty.
        image_ocr=ocr_extractor.read_page if pictures else None,
        max_images=settings.OCR_EMBEDDED_IMAGES_MAX if pictures else 0,
    )
    page_texts = [page.text for page in pages]

    for index, page in enumerate(pages, start=1):
        text = page.text
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=text,
                tables=page.tables,
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


def _document_level(metadata: dict) -> dict:
    return {
        key: value
        for key, value in metadata.items()
        if key not in ("extractor", "extractor_version")
    }


def _extract_sheets(context, pages, metadata) -> StageResult:
    """One page per sheet; a delimited file is one sheet.

    A workbook with nothing in it still yields a page - an empty one - so the
    OCR stage's "some text must exist" check fails it as EMPTY_EXTRACTION,
    the same honest outcome as an empty document of any other type.
    """
    if not pages:
        pages = [spreadsheet.SheetPage()]

    for index, page in enumerate(pages, start=1):
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=page.text,
                tables=page.tables,
                extraction_method=PageContent.ExtractionMethod.SPREADSHEET,
                metadata={
                    "extractor": metadata["extractor"],
                    "extractor_version": metadata["extractor_version"],
                    **{k: v for k, v in page.metadata.items() if k != "extractor"},
                },
            )
        )

    context.metadata.update(_document_level(metadata))

    characters = sum(len(page.text) for page in context.pages)
    return StageResult(
        message=(
            f"Read {characters} characters from {len(context.pages)} page(s) "
            f"({metadata['rows']} rows)."
            + (
                " The file was cut off at the cell limit."
                if metadata["truncated"]
                else ""
            )
        ),
        metadata={"pages": len(context.pages), "characters": characters, **metadata},
    )


def _extract_slides(context) -> StageResult:
    pictures = settings.OCR_EMBEDDED_IMAGES and settings.OCR_EMBEDDED_IMAGES_MAX > 0
    pages, metadata = presentation.extract_slides(
        context.file_path,
        image_ocr=ocr_extractor.read_page if pictures else None,
        max_images=settings.OCR_EMBEDDED_IMAGES_MAX if pictures else 0,
    )

    for index, page in enumerate(pages, start=1):
        context.pages.append(
            ExtractedPage(
                page_number=index,
                text=page.text,
                tables=page.tables,
                extraction_method=PageContent.ExtractionMethod.PRESENTATION,
                metadata={
                    "extractor": metadata["extractor"],
                    "extractor_version": metadata["extractor_version"],
                    **{
                        k: v
                        for k, v in page.metadata.items()
                        if k not in ("extractor", "extractor_version")
                    },
                },
            )
        )

    context.metadata.update(_document_level(metadata))

    characters = sum(len(page.text) for page in context.pages)
    return StageResult(
        message=(
            f"Read {characters} characters from {len(pages)} slide(s) "
            f"({metadata['tables']} table(s), {metadata['charts']} chart(s))."
        ),
        metadata={"pages": len(pages), "characters": characters, **metadata},
    )


def _defer_image_to_ocr(context) -> StageResult:
    # One page per frame. Almost every image has one; a multi-page TIFF is a
    # scanned document and reading only its first page would silently drop
    # the rest.
    frames = ocr_extractor.image_frame_count(context.file_path)
    context.pages = [
        ExtractedPage(page_number=number) for number in range(1, frames + 1)
    ]

    return StageResult(
        message=(
            "Image has no text layer; deferred to OCR."
            if frames == 1
            else f"Image has {frames} frames, no text layer; deferred to OCR."
        ),
        metadata={"pages": frames, "pages_needing_ocr": frames},
    )
