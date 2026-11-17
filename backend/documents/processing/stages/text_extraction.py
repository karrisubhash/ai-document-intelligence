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
from documents.processing.figures import vision
from documents.processing.normalize import strip_nul


def run(context) -> StageResult:
    """Fill in whatever text is available without OCR.

    Pages left with `extraction_method = None` carry no text layer and
    are picked up by the OCR stage - keeping the two paths separately
    measurable rather than hidden behind one fallback.
    """
    # Made once, here, so every stage after this one spends the same budget.
    if context.figure_describer is None:
        context.figure_describer = vision.for_document()

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

    if context.file_type in (FileType.DOC, FileType.XLS, FileType.PPT):
        return _extract_legacy(context)

    if context.file_type in _OTHER_FORMATS:
        return _extract_other(context)

    return _defer_image_to_ocr(context)


# Formats read into the same pages-and-tables shape by their own extractors.
_OTHER_FORMATS = frozenset(
    {
        FileType.HTML,
        FileType.RTF,
        FileType.ODT,
        FileType.ODS,
        FileType.ODP,
        FileType.JSON,
        FileType.XML,
    }
)


def _extract_legacy(context) -> StageResult:
    """An old-format Office file: converted, then read as its modern form.

    The converted file goes through the very extractor a .docx, .xlsx or
    .pptx does, so its tables, charts and pictures are read the same way. The
    original stays the stored file; the conversion is a temporary.
    """
    from documents.processing.extractors import legacy

    kind = context.file_type
    original = context.file_path
    with legacy.converted(original, kind) as modern:
        context.file_path = modern
        try:
            if kind == FileType.DOC:
                result = _extract_docx_text(context)
            elif kind == FileType.XLS:
                result = _extract_sheets(context, *spreadsheet.extract_xlsx(modern))
            else:
                result = _extract_slides(context)
        finally:
            context.file_path = original

    context.metadata["converted_from"] = kind
    result.metadata["converted_from"] = kind
    result.message = f"Converted from an old-format file. {result.message}"
    return result


def _extract_other(context) -> StageResult:
    """Web pages, Rich Text, OpenDocument, JSON and XML.

    Each reads into pages whose text and tables are written through the shared
    builder, so a table's span re-slices out of its page exactly as it does
    for a Word or Excel file. Which stored kind of page they are: a document
    (DOCX_TEXT: structured, with author-declared page breaks at best), a sheet
    or a slide - the kinds that already exist, so nothing downstream needs to
    know a new one.
    """
    from documents.processing.extractors import (
        opendocument,
        rtf,
        structured,
        webpage,
    )

    kind = context.file_type
    pictures = settings.OCR_EMBEDDED_IMAGES and settings.OCR_EMBEDDED_IMAGES_MAX > 0
    picture_options = {
        "image_ocr": ocr_extractor.read_page if pictures else None,
        "max_images": _picture_budget(context, pictures),
        "describe": _describer(context),
    }

    if kind == FileType.HTML:
        page, metadata = webpage.extract_page(context.file_path)
        pages, method = [page], PageContent.ExtractionMethod.DOCX_TEXT
    elif kind == FileType.RTF:
        page, metadata = rtf.extract_page(context.file_path)
        pages, method = [page], PageContent.ExtractionMethod.DOCX_TEXT
    elif kind == FileType.ODT:
        pages, metadata = opendocument.extract_text(
            context.file_path, **picture_options
        )
        method = PageContent.ExtractionMethod.DOCX_TEXT
    elif kind == FileType.ODS:
        return _extract_sheets(context, *opendocument.extract_sheets(context.file_path))
    elif kind == FileType.ODP:
        pages, metadata = opendocument.extract_slides(
            context.file_path, **picture_options
        )
        method = PageContent.ExtractionMethod.PRESENTATION
    elif kind == FileType.JSON:
        name = context.document_version.file_name.lower()
        lines = name.endswith((".jsonl", ".ndjson"))
        pages, metadata = structured.extract_json(context.file_path, lines=lines)
        return _extract_sheets(context, pages, _as_sheet_metadata(pages, metadata))
    else:
        pages, metadata = structured.extract_xml(context.file_path)
        return _extract_sheets(context, pages, _as_sheet_metadata(pages, metadata))

    _note_figures(context)
    for number, page in enumerate(pages, start=1):
        context.pages.append(
            ExtractedPage(
                page_number=number,
                text=page.text,
                tables=page.tables,
                extraction_method=method,
                metadata={
                    "extractor": metadata["extractor"],
                    "extractor_version": metadata["extractor_version"],
                    **{
                        key: value
                        for key, value in getattr(page, "metadata", {}).items()
                        if key not in ("extractor", "extractor_version")
                    },
                },
            )
        )

    context.metadata.update(_document_level(metadata))
    characters = sum(len(page.text) for page in context.pages)
    return StageResult(
        message=(
            f"Read {characters} characters from {len(context.pages)} page(s) "
            f"of a {kind} file ({len(sum((p.tables for p in pages), []))} table(s))."
        ),
        metadata={"pages": len(context.pages), "characters": characters, **metadata},
    )


def _as_sheet_metadata(pages, metadata: dict) -> dict:
    """What `_extract_sheets` reports on: rows and whether the file was cut."""
    return {
        **metadata,
        "rows": sum(page.text.count("\n") + 1 for page in pages if page.text),
        "truncated": bool(metadata.get("truncated", False)),
    }


def _describer(context):
    """`describe(image) -> str` for the extractors, or None without a model."""
    describer = context.figure_describer
    return describer.block_for if describer is not None else None


def _picture_budget(context, pictures: bool) -> int:
    """How many pictures an Office file may read: OCR's budget, or - when a
    model describes them - that or the model's, whichever is larger."""
    ocr = settings.OCR_EMBEDDED_IMAGES_MAX if pictures else 0
    if context.figure_describer is None:
        return ocr
    return max(ocr, settings.FIGURE_DESCRIBE_MAX)


def _note_figures(context) -> None:
    describer = context.figure_describer
    if describer is not None:
        context.metadata.update(describer.metadata())


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
        describe=_describer(context),
        figure_dpi=settings.FIGURE_RENDER_DPI,
    )
    _note_figures(context)

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
        max_images=_picture_budget(context, pictures),
        describe=_describer(context),
    )
    _note_figures(context)
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
        max_images=_picture_budget(context, pictures),
        describe=_describer(context),
    )
    _note_figures(context)

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
