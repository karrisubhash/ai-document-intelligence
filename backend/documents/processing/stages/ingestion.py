import os

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError

# Content sniffing beats the extension: the uploaded name is client-supplied
# and a mislabelled file would otherwise be handed to the wrong extractor.
MAGIC_SIGNATURES = [
    (b"%PDF", FileType.PDF),
    (b"\x89PNG\r\n\x1a\n", FileType.IMAGE),
    (b"\xff\xd8\xff", FileType.IMAGE),
]

EXTENSION_TYPES = {
    ".pdf": FileType.PDF,
    ".txt": FileType.TXT,
    ".png": FileType.IMAGE,
    ".jpg": FileType.IMAGE,
    ".jpeg": FileType.IMAGE,
}


def detect_file_type(file_path: str) -> str | None:
    with open(file_path, "rb") as handle:
        header = handle.read(8)

    for signature, file_type in MAGIC_SIGNATURES:
        if header.startswith(signature):
            return file_type

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
