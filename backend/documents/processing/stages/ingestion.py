import codecs
import os

from django.conf import settings

from documents.processing.context import FileType, StageResult
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import legacy, ooxml, opendocument

# Content sniffing beats the extension: the uploaded name is client-supplied
# and a mislabelled file would otherwise be handed to the wrong extractor.
MAGIC_SIGNATURES = [
    (b"%PDF", FileType.PDF),
    (b"\x89PNG\r\n\x1a\n", FileType.IMAGE),
    (b"\xff\xd8\xff", FileType.IMAGE),
    # TIFF, little- and big-endian. Often multi-page: a scanned document.
    (b"II*\x00", FileType.IMAGE),
    (b"MM\x00*", FileType.IMAGE),
    # Rich Text is the one text format with a signature of its own.
    (b"{\\rtf", FileType.RTF),
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
    ".rtf": FileType.RTF,
    ".odt": FileType.ODT,
    ".ods": FileType.ODS,
    ".odp": FileType.ODP,
    ".html": FileType.HTML,
    ".htm": FileType.HTML,
    ".xhtml": FileType.HTML,
    ".json": FileType.JSON,
    ".jsonl": FileType.JSON,
    ".ndjson": FileType.JSON,
    ".xml": FileType.XML,
    ".doc": FileType.DOC,
    ".xls": FileType.XLS,
    ".ppt": FileType.PPT,
    # Text by another name. Read as text, in whatever encoding Windows or an
    # editor wrote them.
    ".md": FileType.TXT,
    ".markdown": FileType.TXT,
    ".rst": FileType.TXT,
    ".log": FileType.TXT,
    ".text": FileType.TXT,
    ".yaml": FileType.TXT,
    ".yml": FileType.TXT,
    ".ini": FileType.TXT,
    ".cfg": FileType.TXT,
    ".conf": FileType.TXT,
    ".toml": FileType.TXT,
    ".tex": FileType.TXT,
    ".srt": FileType.TXT,
    ".vtt": FileType.TXT,
    ".sql": FileType.TXT,
}

# Declared types that are read as text, so a binary file renamed to one of
# them is refused rather than "read" into rows of noise.
TEXT_FORMATS = frozenset({FileType.CSV, FileType.HTML, FileType.JSON, FileType.XML})


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

    # Office's binary formats share one container signature; the extension
    # says which of the three it is. (A .doc that is really Rich Text matched
    # its own signature above, and one that is plain text or a saved web page
    # is handled with the text formats below.)
    if header.startswith(legacy.OLE_SIGNATURE):
        return legacy.EXTENSIONS.get(os.path.splitext(file_path)[1].lower())

    if header.startswith(ZIP_SIGNATURES):
        # The type is whichever main part the archive holds. A plain .zip, or
        # an .odt, holds none of them and comes back unrecognised rather than
        # being handed to an extractor that cannot read it.
        part = ooxml.first_part(file_path, OOXML_MAIN_PARTS)
        if part is not None:
            return OOXML_MAIN_PARTS[part]
        # OpenDocument names its type in a `mimetype` entry instead.
        return {
            "ODT": FileType.ODT,
            "ODS": FileType.ODS,
            "ODP": FileType.ODP,
        }.get(opendocument.kind_of(file_path))

    # No signature matched. Only text formats may fall back to the
    # extension, since they have no magic bytes.
    declared = EXTENSION_TYPES.get(os.path.splitext(file_path)[1].lower())
    if declared == FileType.TXT:
        return FileType.TXT
    # A delimited, web, JSON or XML file is only believed to be one if it is
    # text: a binary blob renamed .csv would otherwise be "parsed" into rows
    # of noise.
    if declared in TEXT_FORMATS and _is_text(header):
        return declared

    # An old-format name on a file that is not an OLE container. Excel writes
    # "web pages" and tab-separated exports under .xls, and Word saves HTML or
    # plain text under .doc; what they are is read off the bytes.
    if declared in legacy.TARGETS and _is_text(header):
        lowered = header[:2048].lstrip().lower()
        if lowered.startswith((b"<!doctype html", b"<html", b"<table", b"<?xml")) or (
            b"<html" in lowered or b"<table" in lowered
        ):
            return FileType.HTML
        return FileType.CSV if declared == FileType.XLS else FileType.TXT

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
