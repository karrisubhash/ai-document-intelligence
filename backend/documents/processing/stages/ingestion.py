import os

from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import docx

# Content sniffing beats the extension: the uploaded name is client-supplied
# and a mislabelled file would otherwise be handed to the wrong extractor.
MAGIC_SIGNATURES = [
    (b"%PDF", FileType.PDF),
    (b"\x89PNG\r\n\x1a\n", FileType.IMAGE),
    (b"\xff\xd8\xff", FileType.IMAGE),
]

# Every ZIP-based office format shares these bytes, so the signature alone
# cannot identify a DOCX - the archive has to be opened and asked whether it
# holds a Word document part.
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

EXTENSION_TYPES = {
    ".pdf": FileType.PDF,
    ".txt": FileType.TXT,
    ".png": FileType.IMAGE,
    ".jpg": FileType.IMAGE,
    ".jpeg": FileType.IMAGE,
    ".docx": FileType.DOCX,
}


def detect_file_type(file_path: str) -> str | None:
    with open(file_path, "rb") as handle:
        header = handle.read(8)

    for signature, file_type in MAGIC_SIGNATURES:
        if header.startswith(signature):
            return file_type

    if header.startswith(ZIP_SIGNATURES):
        # A .xlsx, .pptx or plain .zip reaches here too and must come back
        # unrecognised rather than be handed to the DOCX extractor.
        return FileType.DOCX if docx.is_docx(file_path) else None

    # No signature matched. Only a plain-text extension may fall back to
    # the extension, since text files have no magic bytes.
    extension = os.path.splitext(file_path)[1].lower()
    if EXTENSION_TYPES.get(extension) == FileType.TXT:
        return FileType.TXT

    return None


def run(context) -> StageResult:
    if not os.path.exists(context.file_path):
        raise StageError(
            "Stored file is missing from storage.",
            code=ErrorCode.MISSING_FILE,
        )

    size_bytes = os.path.getsize(context.file_path)
    if size_bytes == 0:
        raise StageError("Uploaded file is empty.", code=ErrorCode.EMPTY_FILE)

    # The size limit is enforced here, before the file is sniffed, not only
    # in VALIDATION. Detecting a ZIP-based type means parsing an
    # attacker-controlled central directory, and an oversized archive full
    # of entries should be refused before any of its structure is read.
    if size_bytes > settings.MAX_DOCUMENT_SIZE_BYTES:
        raise StageError(
            f"File is {size_bytes} bytes, over the "
            f"{settings.MAX_DOCUMENT_SIZE_BYTES} byte limit.",
            code=ErrorCode.FILE_TOO_LARGE,
        )

    context.file_type = detect_file_type(context.file_path)
    context.metadata["size_bytes"] = size_bytes

    declared = EXTENSION_TYPES.get(
        os.path.splitext(context.document_version.file_name)[1].lower()
    )

    return StageResult(
        message=f"Inspected {size_bytes} bytes, detected {context.file_type or 'UNKNOWN'}.",
        metadata={
            "size_bytes": size_bytes,
            "detected_type": context.file_type,
            "declared_type": declared,
            "type_matches_extension": context.file_type == declared,
        },
    )
