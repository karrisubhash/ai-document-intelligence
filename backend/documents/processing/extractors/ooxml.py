"""What every Office Open XML format has in common: it is a ZIP archive.

DOCX, XLSX and PPTX are all ZIP files, and three things follow from that
that the format-specific extractors would otherwise each have to get right.

*They share their magic bytes.* "PK\\x03\\x04" says "ZIP", not "Word". The
type is whichever main part the archive holds - `word/document.xml`,
`xl/workbook.xml`, `ppt/presentation.xml` - so detection opens the archive
and asks. A malformed archive answers "not this type"; a storage error does
NOT, because reporting a blip as a permanent UNSUPPORTED_FILE_TYPE would
turn a retryable condition into one that is never retried.

*ZIP archives from untrusted users can be decompression bombs.* The size
checked at the INGESTION stage is the compressed size, which says nothing
about what it expands to, so the uncompressed total and the ratio are
checked before anything is parsed.

*Reading a part must not trust its XML.* Parts are parsed with entity
resolution and network access off, and a single part is read only up to a
cap, so a hostile file cannot make a worker expand an entity bomb or pull a
gigabyte into memory through one small-looking entry.
"""

import zipfile

from lxml import etree

from documents.processing.errors import ErrorCode, StageError, TransientError

# A file that expands to more than this is refused unparsed. Generous against
# real documents - a 50 MB upload of dense text expands nowhere near it - and
# far below what a bomb needs to exhaust a worker.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

# Refuse absurd expansion even when the absolute total is acceptable.
MAX_COMPRESSION_RATIO = 200

# One XML part, read whole into memory to be parsed. Real parts of a very
# large document are tens of megabytes; this is the cap, not the expectation.
MAX_PART_BYTES = 128 * 1024 * 1024


def has_part(file_path: str, part: str) -> bool:
    """Whether this ZIP holds `part` (e.g. "word/document.xml").

    A malformed archive answers False. An OSError is deliberately NOT
    swallowed - see the module docstring.
    """
    try:
        with zipfile.ZipFile(file_path) as archive:
            return part in archive.namelist()
    except zipfile.BadZipFile:
        return False
    except OSError as exc:
        raise TransientError(
            f"Could not read the stored file while detecting its type: {exc}"
        ) from exc


def first_part(file_path: str, candidates) -> str | None:
    """The first of `candidates` this ZIP holds, or None.

    The format of an OOXML file is whichever main part it carries, so one
    look at the archive answers it. Same error rule as `has_part`.
    """
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None
    except OSError as exc:
        raise TransientError(
            f"Could not read the stored file while detecting its type: {exc}"
        ) from exc
    return next((part for part in candidates if part in names), None)


def check_expansion(file_path: str, label: str = "Office") -> dict:
    """Reject decompression bombs before handing the file to a parser."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            entries = archive.infolist()
            uncompressed = sum(entry.file_size for entry in entries)
            compressed = sum(entry.compress_size for entry in entries) or 1
    except (zipfile.BadZipFile, OSError) as exc:
        raise StageError(
            f"Could not read {label} archive: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    ratio = uncompressed / compressed

    if uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise StageError(
            f"{label} expands to {uncompressed} bytes, over the "
            f"{MAX_UNCOMPRESSED_BYTES} byte limit.",
            code=ErrorCode.FILE_TOO_LARGE,
        )

    if ratio > MAX_COMPRESSION_RATIO:
        raise StageError(
            f"{label} expands {ratio:.0f}x, over the {MAX_COMPRESSION_RATIO}x "
            f"limit. Refusing to parse a suspected decompression bomb.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )

    return {
        "entries": len(entries),
        "compressed_bytes": compressed,
        "uncompressed_bytes": uncompressed,
        "compression_ratio": round(ratio, 1),
    }


def parser() -> etree.XMLParser:
    """An XML parser for untrusted parts: no entities, no network, no DTD."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_blank_text=False,
    )


def read_part(file_path: str, part: str):
    """The parsed XML root of one part, or None when the archive lacks it."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            try:
                info = archive.getinfo(part)
            except KeyError:
                return None
            if info.file_size > MAX_PART_BYTES:
                raise StageError(
                    f"{part} is {info.file_size} bytes, over the "
                    f"{MAX_PART_BYTES} byte limit for one part.",
                    code=ErrorCode.FILE_TOO_LARGE,
                )
            data = archive.read(part)
    except (zipfile.BadZipFile, OSError) as exc:
        raise StageError(
            f"Could not read {part}: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    try:
        return etree.fromstring(data, parser())
    except etree.XMLSyntaxError as exc:
        raise StageError(
            f"{part} is not valid XML: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc


def local(element) -> str:
    """An element's tag without its namespace; "" for comments and the like."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]
