from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import (
    docx,
    ooxml,
    opendocument,
    pdf,
    presentation,
)
from documents.processing.extractors import ocr as ocr_extractor

SUPPORTED_TYPES = {
    FileType.PDF,
    FileType.TXT,
    FileType.IMAGE,
    FileType.DOCX,
    FileType.XLSX,
    FileType.CSV,
    FileType.PPTX,
    FileType.HTML,
    FileType.RTF,
    FileType.ODT,
    FileType.ODS,
    FileType.ODP,
    FileType.JSON,
    FileType.XML,
    FileType.DOC,
    FileType.XLS,
    FileType.PPT,
}


def run(context) -> StageResult:
    if context.file_type not in SUPPORTED_TYPES:
        raise StageError(
            f"Unsupported file type for '{context.document_version.file_name}'. "
            f"Supported: PDF, Word (DOCX), Excel (XLSX), PowerPoint (PPTX), "
            f"old Word, Excel and PowerPoint (DOC, XLS, PPT), "
            f"OpenDocument (ODT, ODS, ODP), RTF, HTML, CSV, TSV, TXT, "
            f"Markdown, JSON, XML, and images (PNG, JPG, TIFF, WebP, BMP).",
            code=ErrorCode.UNSUPPORTED_FILE_TYPE,
        )

    size_bytes = context.metadata["size_bytes"]
    if size_bytes > settings.MAX_DOCUMENT_SIZE_BYTES:
        raise StageError(
            f"File is {size_bytes} bytes, over the "
            f"{settings.MAX_DOCUMENT_SIZE_BYTES} byte limit.",
            code=ErrorCode.FILE_TOO_LARGE,
        )

    metadata = {"file_type": context.file_type, "size_bytes": size_bytes}

    if context.file_type == FileType.PDF:
        # Also proves the PDF actually parses before we commit to extraction.
        count = pdf.page_count(context.file_path)

        if count == 0:
            raise StageError(
                "PDF contains no pages.",
                code=ErrorCode.CORRUPT_DOCUMENT,
            )

        _check_page_limit(count, "PDF", "pages")

        metadata["page_count"] = count

    if context.file_type == FileType.DOCX:
        # Cheap structural checks only. A page limit is not applied here: a
        # DOCX has no page count until the whole document has been walked,
        # so enforcing one would mean parsing the file twice. The
        # decompression-bomb guard inside the extractor is what bounds the
        # work, and it runs before any parsing happens.
        metadata.update(docx.check_expansion(context.file_path))

    if context.file_type == FileType.XLSX:
        # Cells are capped as they are read; this bounds the expansion that
        # happens before the first one is.
        metadata.update(ooxml.check_expansion(context.file_path, "XLSX"))

    if context.file_type in (FileType.ODT, FileType.ODS, FileType.ODP):
        # The same archive guard, before anything is parsed.
        metadata.update(ooxml.check_expansion(context.file_path, "OpenDocument"))
        if context.file_type == FileType.ODP:
            count = opendocument.slide_count(context.file_path)
            if count == 0:
                raise StageError(
                    "Presentation contains no slides.",
                    code=ErrorCode.CORRUPT_DOCUMENT,
                )
            _check_page_limit(count, "Presentation", "slides")
            metadata["page_count"] = count

    if context.file_type == FileType.PPTX:
        metadata.update(ooxml.check_expansion(context.file_path, "PPTX"))
        count = presentation.slide_count(context.file_path)

        if count == 0:
            raise StageError(
                "Presentation contains no slides.",
                code=ErrorCode.CORRUPT_DOCUMENT,
            )

        _check_page_limit(count, "Presentation", "slides")

        metadata["page_count"] = count

    if context.file_type == FileType.IMAGE:
        # A multi-page TIFF is one page per frame, so a page limit, when one
        # is set, applies to it too.
        frames = ocr_extractor.image_frame_count(context.file_path)
        _check_page_limit(frames, "Image", "frames")

        metadata["page_count"] = frames

    return StageResult(message="Validated.", metadata=metadata)


def _check_page_limit(count: int, kind: str, unit: str) -> None:
    """Refuse a document longer than MAX_DOCUMENT_PAGES, when one is set.

    0, the default, is no limit: a document of any length is read, bounded
    by its size (MAX_DOCUMENT_SIZE_BYTES) and the task time limit instead.
    """
    limit = settings.MAX_DOCUMENT_PAGES
    if limit and count > limit:
        raise StageError(
            f"{kind} has {count} {unit}, over the {limit} page limit.",
            code=ErrorCode.TOO_MANY_PAGES,
        )
