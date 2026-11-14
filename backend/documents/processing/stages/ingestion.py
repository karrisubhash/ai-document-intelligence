import codecs
import os

from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import ooxml

# Content sniffing beats the extension: the uploaded name is client-supplied
# and a mislabelled file would otherwise be handed to the wrong extractor.
MAGIC_SIGNATURES = [
    (b"%PDF", FileType.PDF),
    (b"\x89PNG\r\n\x1a\n", FileType.IMAGE),
    (b"\xff\xd8\xff", FileType.IMAGE),
    # TIFF, little- and big-endian. Often multi-page: a scanned document.
    (b"II*\x00", FileType.IMAGE),
    (b"MM\x00*", FileType.IMAGE),
]

# Signatures that need more than a prefix to be sure of. Two bytes ("BM") is
# also how a text file that begins with those letters starts, so a bitmap must
# also carry a plausible DIB header; WebP is a RIFF container whose type sits
# four bytes later.
BMP_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


def _is_bmp(header: bytes) -> bool:
    if not header.startswith(b"BM") or len(header) < 18:
        return False
    return int.from_bytes(header[14:18], "little") in BMP_HEADER_SIZES


def _is_webp(header: bytes) -> bool:
    return header.startswith(b"RIFF") and header[8:12] == b"WEBP"


# The main part each ZIP-based office format carries. The bytes alone cannot
# tell them apart, so the archive is asked which of these it holds.
OOXML_MAIN_PARTS = {
    "word/document.xml": FileType.DOCX,
    "xl/workbook.xml": FileType.XLSX,
    "ppt/presentation.xml": FileType.PPTX,
}

# How much of a file is read to sniff it: enough for every signature above and
# for a delimited file to be checked for being text at all.
SNIFF_BYTES = 4096

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
    ".tif": FileType.IMAGE,
    ".tiff": FileType.IMAGE,
    ".webp": FileType.IMAGE,
    ".bmp": FileType.IMAGE,
    ".docx": FileType.DOCX,
    ".xlsx": FileType.XLSX,
    ".pptx": FileType.PPTX,
    ".csv": FileType.CSV,
    ".tsv": FileType.CSV,
}


def _is_text(sample: bytes) -> bool:
    """Whether bytes read as text: no NUL, unless it is UTF-16 with a BOM."""
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    return bytes(1) not in sample


def detect_file_type(file_path: str) -> str | None:
    with open(file_path, "rb") as handle:
        header = handle.read(SNIFF_BYTES)

    for signature, file_type in MAGIC_SIGNATURES:
        if header.startswith(signature):
            return file_type

    if _is_webp(header) or _is_bmp(header):
        return FileType.IMAGE

    if header.startswith(ZIP_SIGNATURES):
        # The type is whichever main part the archive holds. A plain .zip, or
        # an .odt, holds none of them and comes back unrecognised rather than
        # being handed to an extractor that cannot read it.
        part = ooxml.first_part(file_path, OOXML_MAIN_PARTS)
        return OOXML_MAIN_PARTS.get(part)

    # No signature matched. Only text formats may fall back to the
    # extension, since they have no magic bytes.
    declared = EXTENSION_TYPES.get(os.path.splitext(file_path)[1].lower())
    if declared == FileType.TXT:
        return FileType.TXT
    # A delimited file is only believed to be one if it is text: a binary
    # blob renamed .csv would otherwise be "parsed" into rows of noise.
    if declared == FileType.CSV and _is_text(header):
        return FileType.CSV

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
