"""DOCX text extraction.

Two things about this format need stating, because both are easy to get
quietly wrong.

*A DOCX has no page count.* Pagination in Word is a rendering result - it
depends on the fonts, page size and printer metrics of whoever opens the
file - so there is no page number to read out of the bytes. What the file
does record is where the author asked for a page break. This extractor
splits on those and labels the result `explicit_breaks` in metadata, so a
reader can tell an author-declared boundary from a rendered one. A document
with no breaks is one page, labelled `none`. Claiming anything more precise
would be inventing structure the file does not contain.

*A DOCX is a ZIP archive*, and ZIP archives from untrusted users can be
decompression bombs. The size checked at the VALIDATION stage is the
compressed size, which says nothing about what it expands to, so the
uncompressed total is checked here before anything is parsed.
"""

import zipfile

from documents.processing.errors import ErrorCode, StageError, TransientError

# The part every real Word document has. Its presence is what separates a
# .docx from the other ZIP-based office formats (.xlsx, .pptx, .odt), which
# share the same magic bytes and would otherwise be handed to this extractor.
DOCUMENT_PART = "word/document.xml"

# A DOCX that expands to more than this is refused unparsed. Generous
# against real documents - a 50 MB upload of dense text expands nowhere near
# it - and far below what a bomb needs to exhaust a worker.
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

# Refuse absurd expansion even when the absolute total is acceptable.
MAX_COMPRESSION_RATIO = 200

VERSION = ""


def _lazy_docx():
    """Imported on use so the module can be read without python-docx
    installed, matching how the OCR extractor treats tesseract."""
    global VERSION
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise StageError(
            "python-docx is required to read DOCX files: "
            "pip install -r backend/requirements.txt",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    VERSION = getattr(docx, "__version__", "") or "unknown"
    return docx


def is_docx(file_path: str) -> bool:
    """Whether this ZIP is specifically a Word document.

    A malformed archive answers False - "not a DOCX" is the correct answer
    about a file that is not a readable ZIP. An OSError is deliberately NOT
    swallowed: a storage blip is a transient condition, and reporting it as
    "not a DOCX" would turn it into a permanent UNSUPPORTED_FILE_TYPE that
    is never retried, which is exactly the wrong side of this project's
    permanent-vs-transient split.
    """
    try:
        with zipfile.ZipFile(file_path) as archive:
            return DOCUMENT_PART in archive.namelist()
    except zipfile.BadZipFile:
        return False
    except OSError as exc:
        raise TransientError(
            f"Could not read the stored file while detecting its type: {exc}"
        ) from exc


def check_expansion(file_path: str) -> dict:
    """Reject decompression bombs before handing the file to a parser."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            entries = archive.infolist()
            uncompressed = sum(entry.file_size for entry in entries)
            compressed = sum(entry.compress_size for entry in entries) or 1
    except (zipfile.BadZipFile, OSError) as exc:
        raise StageError(
            f"Could not read DOCX archive: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    ratio = uncompressed / compressed

    if uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise StageError(
            f"DOCX expands to {uncompressed} bytes, over the "
            f"{MAX_UNCOMPRESSED_BYTES} byte limit.",
            code=ErrorCode.FILE_TOO_LARGE,
        )

    if ratio > MAX_COMPRESSION_RATIO:
        raise StageError(
            f"DOCX expands {ratio:.0f}x, over the {MAX_COMPRESSION_RATIO}x "
            f"limit. Refusing to parse a suspected decompression bomb.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )

    return {
        "entries": len(entries),
        "compressed_bytes": compressed,
        "uncompressed_bytes": uncompressed,
        "compression_ratio": round(ratio, 1),
    }


def _table_text(table) -> str:
    """A table as lines of tab-separated cells.

    Tables carry real content in institutional documents - fee tables,
    course lists, signature blocks - and python-docx's `paragraphs` skips
    them entirely, so iterating the body in document order is the only way
    to keep the text in the order a reader would meet it.
    """
    lines = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        if any(cells):
            lines.append("\t".join(cells))
    return "\n".join(lines)


def _blocks(document, docx_module):
    """Paragraph and table text in document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            yield "paragraph", paragraph
        elif tag == "tbl":
            yield "table", Table(child, document)


def _has_page_break(paragraph) -> bool:
    """Whether the author put an explicit page break in this paragraph."""
    for run in paragraph.runs:
        for break_element in run._element.findall(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br"
        ):
            if (
                break_element.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type"
                )
                == "page"
            ):
                return True
    return False


def extract_pages(file_path: str) -> tuple:
    """Returns (list of page texts, metadata).

    Page boundaries are the author's explicit page breaks. See the module
    docstring for why that is the most this format honestly supports.
    """
    docx_module = _lazy_docx()
    expansion = check_expansion(file_path)

    try:
        document = docx_module.Document(file_path)
    except Exception as exc:
        raise StageError(
            f"Could not read DOCX: {exc}",
            code=ErrorCode.CORRUPT_DOCUMENT,
        ) from exc

    pages = []
    current = []
    tables = 0
    paragraphs = 0

    try:
        for kind, block in _blocks(document, docx_module):
            if kind == "table":
                tables += 1
                text = _table_text(block)
                if text:
                    current.append(text)
                continue

            paragraphs += 1
            if _has_page_break(block) and current:
                pages.append("\n".join(current))
                current = []

            text = block.text.strip()
            if text:
                current.append(text)
    except Exception as exc:
        raise StageError(
            f"DOCX text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    pages.append("\n".join(current))

    return pages, {
        "extractor": "python-docx",
        "extractor_version": VERSION,
        "paragraphs": paragraphs,
        "tables": tables,
        "pagination": "explicit_breaks" if len(pages) > 1 else "none",
        **expansion,
    }


def page_count(file_path: str) -> int:
    """Derived page count, for the VALIDATION stage's limit check.

    Requires a full parse, because the number does not exist until the
    breaks have been walked.
    """
    pages, _ = extract_pages(file_path)
    return len(pages)
