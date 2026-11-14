from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import docx, ooxml, pdf, presentation
from documents.processing.extractors import ocr as ocr_extractor

SUPPORTED_TYPES = {
    FileType.PDF,
    FileType.TXT,
    FileType.IMAGE,
    FileType.DOCX,
    FileType.XLSX,
    FileType.CSV,
    FileType.PPTX,
}


def run(context) -> StageResult:
    if context.file_type not in SUPPORTED_TYPES:
        raise StageError(
            f"Unsupported file type for '{context.document_version.file_name}'. "
            f"Supported: PDF, DOCX, XLSX, PPTX, CSV, TXT, PNG, JPG, TIFF, "
            f"WebP, BMP.",
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

        if count > settings.MAX_DOCUMENT_PAGES:
            raise StageError(
                f"PDF has {count} pages, over the "
                f"{settings.MAX_DOCUMENT_PAGES} page limit.",
                code=ErrorCode.TOO_MANY_PAGES,
            )

        metadata["page_count"] = count

    if context.file_type == FileType.DOCX:
        # Cheap structural checks only. The page limit is not applied here:
        # a DOCX has no page count until the whole document has been walked,
        # so enforcing it would mean parsing the file twice. The
        # decompression-bomb guard inside the extractor is what bounds the
        # work, and it runs before any parsing happens.
        metadata.update(docx.check_expansion(context.file_path))

    if context.file_type == FileType.XLSX:
        # Cells are capped as they are read; this bounds the expansion that
        # happens before the first one is.
        metadata.update(ooxml.check_expansion(context.file_path, "XLSX"))

    if context.file_type == FileType.PPTX:
        metadata.update(ooxml.check_expansion(context.file_path, "PPTX"))
        count = presentation.slide_count(context.file_path)

        if count == 0:
            raise StageError(
                "Presentation contains no slides.",
                code=ErrorCode.CORRUPT_DOCUMENT,
            )

        if count > settings.MAX_DOCUMENT_PAGES:
            raise StageError(
                f"Presentation has {count} slides, over the "
                f"{settings.MAX_DOCUMENT_PAGES} page limit.",
                code=ErrorCode.TOO_MANY_PAGES,
            )

        metadata["page_count"] = count

    if context.file_type == FileType.IMAGE:
        # A multi-page TIFF is one page per frame, so it has a page limit too.
        frames = ocr_extractor.image_frame_count(context.file_path)

        if frames > settings.MAX_DOCUMENT_PAGES:
            raise StageError(
                f"Image has {frames} frames, over the "
                f"{settings.MAX_DOCUMENT_PAGES} page limit.",
                code=ErrorCode.TOO_MANY_PAGES,
            )

        metadata["page_count"] = frames

    return StageResult(message="Validated.", metadata=metadata)
