from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import pdf

SUPPORTED_TYPES = {FileType.PDF, FileType.TXT, FileType.IMAGE}


def run(context) -> StageResult:
    if context.file_type not in SUPPORTED_TYPES:
        raise StageError(
            f"Unsupported file type for '{context.document_version.file_name}'. "
            f"Supported: PDF, TXT, PNG, JPG, JPEG.",
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

    return StageResult(message="Validated.", metadata=metadata)
