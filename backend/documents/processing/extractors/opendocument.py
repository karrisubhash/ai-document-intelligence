"""OpenDocument files: .odt text, .ods spreadsheets and .odp presentations.

LibreOffice, OpenOffice and Google Docs' "Download as OpenDocument" all write
this family, and it is common in institutions that avoid Microsoft Office. It
is a ZIP holding `content.xml`, which this module reads the way the Office Open
XML extractors read theirs:

* text (.odt): headings, paragraphs and lists in order, tables as tables,
  text boxes, the text of pictures through the same reader the Word extractor
  uses, and charts as the table of numbers stored beside them;
* spreadsheets (.ods): a page per visible sheet through the shared sheet
  builder, values as displayed, repeated rows and columns expanded within the
  same limits;
* presentations (.odp): a page per slide, title first, with its notes.

Nothing is executed and the XML is parsed with entities and the network off
(ooxml.parser); the archive is checked for decompression bombs first.
"""

import zipfile
from dataclasses import dataclass, field

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, ooxml, spreadsheet
from documents.processing.extractors.ooxml import local
from documents.processing.extractors.pictures import PictureReader

SOURCE = "odf:content"
VERSION = "1"

MIMETYPES = {
    "application/vnd.oasis.opendocument.text": "ODT",
    "application/vnd.oasis.opendocument.spreadsheet": "ODS",
    "application/vnd.oasis.opendocument.presentation": "ODP",
}

OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
XLINK = "http://www.w3.org/1999/xlink"
PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"

# Expanded repeats are bounded: a spreadsheet writes "this empty cell, a
# million times" as one element.
MAX_REPEAT = 100
MAX_ROWS_PER_TABLE = 20_000


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def kind_of(file_path: str) -> str | None:
    """ "ODT", "ODS" or "ODP" for an OpenDocument file, else None."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            if "mimetype" not in archive.namelist():
                return None
            info = archive.getinfo("mimetype")
            if info.file_size > 256:
                return None
            name = archive.read("mimetype").decode("ascii", "ignore").strip()
    except zipfile.BadZipFile:
        return None
    except OSError:
        return None
    return MIMETYPES.get(name)


@dataclass
class OdfPage:
    text: str = ""
    tables: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _content(file_path: str):
    root = ooxml.read_part(file_path, "content.xml")
    if root is None:
        raise StageError(
            "The OpenDocument file has no content.xml.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )
    return root


def _count(element, attribute: str, limit: int = MAX_REPEAT) -> int:
    try:
        return max(1, min(int(element.get(_q(TABLE, attribute)) or 1), limit))
    except ValueError:
        return 1


class _Reader:
    def __init__(self, file_path: str, pictures: PictureReader):
        self.file_path = file_path
        self.pictures = pictures
        self.charts = 0
        self.tables = 0
        self.text_boxes = 0
        self.paragraphs = 0

    # -- inline text ---------------------------------------------------

    def inline(self, element, out: list) -> None:
        if element.text:
            out.append(element.text)
        for child in element:
            name = local(child)
            if name == "s":
                try:
                    out.append(" " * min(int(child.get(_q(TEXT, "c")) or 1), 50))
                except ValueError:
                    out.append(" ")
            elif name == "tab":
                out.append("\t")
            elif name == "line-break":
                out.append("\n")
            elif name in ("span", "a", "meta", "ruby", "ruby-base", "bookmark-ref"):
                self.inline(child, out)
            elif name in ("note", "annotation", "bookmark", "bookmark-start"):
                pass  # footnote bodies and review comments are not the line's text
            elif name in ("frame", "custom-shape"):
                self._frame_inline(child, out)
            if child.tail:
                out.append(child.tail)

    def text_of(self, element) -> str:
        pieces: list = []
        self.inline(element, pieces)
        parts = "".join(pieces).split(chr(10))
        return chr(10).join(" ".join(part.split()) for part in parts).strip()

    def _frame_inline(self, frame, out: list) -> None:
        """A picture's alternative text, inline; its boxes come out as blocks
        after the paragraph (see `frames_of`)."""
        for child in frame:
            if local(child) in ("title", "desc") and child.text:
                alt = " ".join(child.text.split())
                if alt:
                    out.append(f" {alt} ")
                    return

    def frames_of(self, element) -> list:
        """The frames directly in `element`, not those inside another frame:
        a frame's own content is read when the frame is, and reading a nested
        one here as well would say it twice."""
        found: list = []

        def walk(node):
            for child in node:
                if local(child) in ("frame", "custom-shape"):
                    found.append(child)
                else:
                    walk(child)

        walk(element)
        return found

    # -- blocks --------------------------------------------------------

    def run(self, parent, builder) -> None:
        for child in parent:
            name = local(child)
            if name == "h":
                self._heading(child, builder)
            elif name == "p":
                self._paragraph(child, builder)
            elif name == "list":
                self._list(child, builder, 0)
            elif name == "table":
                self._table(child, builder)
            elif name in ("section", "index-body", "text-box", "frame", "page"):
                self.run(child, builder)
            elif name in ("table-of-content", "alphabetical-index", "bibliography"):
                # An index lists what the document already says, with page
                # numbers that mean nothing here.
                continue

    def _paragraph(self, element, builder) -> None:
        self.paragraphs += 1
        text = self.text_of(element)
        if text:
            builder.add_text(text)
        self._frames(element, builder)

    def _heading(self, element, builder) -> None:
        text = self.text_of(element)
        if text:
            builder.add_text(text)
        self._frames(element, builder)

    def _list(self, element, builder, depth: int) -> None:
        number = 0
        for item in element:
            if local(item) not in ("list-item", "list-header"):
                continue
            number += 1
            first = True
            for child in item:
                name = local(child)
                if name in ("p", "h"):
                    text = self.text_of(child)
                    if text:
                        marker = "-" if first else " "
                        builder.add_text(("  " * depth) + f"{marker} {text}")
                        first = False
                    self._frames(child, builder)
                elif name == "list":
                    self._list(child, builder, depth + 1)

    def _frames(self, element, builder) -> None:
        """What a paragraph's frames hold: text boxes, tables, charts, pictures."""
        for frame in self.frames_of(element):
            for box in frame.iter(_q(DRAW, "text-box")):
                self.text_boxes += 1
                self.run(box, builder)
            for obj in frame.iter(_q(DRAW, "object")):
                self._chart(obj, builder)
            for image in frame.iter(_q(DRAW, "image")):
                self._picture(image, builder)

    def _picture(self, image, builder) -> None:
        href = image.get(_q(XLINK, "href")) or ""
        if not href or href.startswith(("http:", "https:", "..")):
            return
        try:
            with zipfile.ZipFile(self.file_path) as archive:
                blob = archive.read(href.lstrip("./"))
        except (KeyError, zipfile.BadZipFile, OSError):
            return
        text = self.pictures.text(blob)
        if text:
            builder.add_text(text)

    def _chart(self, obj, builder) -> None:
        """An embedded chart is a sub-document whose own `table:table` holds
        the numbers it was drawn from."""
        href = (obj.get(_q(XLINK, "href")) or "").lstrip("./")
        if not href or href.startswith(("http:", "https:")):
            return
        root = ooxml.read_part(self.file_path, f"{href.rstrip('/')}/content.xml")
        if root is None:
            return
        table = next(root.iter(_q(TABLE, "table")), None)
        if table is None:
            return
        rows = self.table_rows(table)
        if len(rows) < 2:
            return
        self.charts += 1
        title = ""
        for element in root.iter(_q(TEXT, "p")):
            title = self.text_of(element)
            if title:
                break
        if title:
            builder.add_text(title)
        builder.add_table(rows, header=True, source="odf:chart")

    # -- tables --------------------------------------------------------

    def _table(self, element, builder) -> None:
        rows = self.table_rows(element)
        if not rows:
            return
        self.tables += 1
        builder.add_table(rows)

    def table_rows(self, table) -> list:
        """The grid of a `table:table`: repeats expanded, a rowspan's label
        filled down, a colspan's left in its first cell."""
        grid: list = []
        pending: dict = {}  # column -> [text, rows left]
        for row in self._rows(table):
            repeat = _count(row, "number-rows-repeated")
            cells = self._row_cells(row, pending)
            if not any(cells):
                continue
            for _ in range(repeat if any(cells) else 1):
                grid.append(list(cells))
            if len(grid) > MAX_ROWS_PER_TABLE:
                break
        return grid

    def _rows(self, table):
        for child in table:
            name = local(child)
            if name == "table-row":
                yield child
            elif name in ("table-row-group", "table-header-rows", "table-rows"):
                yield from self._rows(child)

    def _row_cells(self, row, pending: dict) -> list:
        cells: list = []
        column = 0
        for cell in row:
            name = local(cell)
            if name not in ("table-cell", "covered-table-cell"):
                continue
            repeat = _count(cell, "number-columns-repeated")
            text = self.cell_text(cell) if name == "table-cell" else ""
            down = _count(cell, "number-rows-spanned", 50)
            for _ in range(repeat):
                while column in pending:
                    filler, left = pending[column]
                    cells.append(filler)
                    if left <= 1:
                        del pending[column]
                    else:
                        pending[column][1] = left - 1
                    column += 1
                cells.append(text)
                if down > 1 and _is_label(text):
                    pending[column] = [text, down - 1]
                column += 1
        return cells

    def cell_text(self, cell) -> str:
        parts = []
        for child in cell:
            if local(child) in ("p", "h"):
                text = self.text_of(child)
                if text:
                    parts.append(text)
            elif local(child) == "list":
                for paragraph in child.iter(_q(TEXT, "p")):
                    text = self.text_of(paragraph)
                    if text:
                        parts.append(text)
            elif local(child) == "table":
                for sub in child.iter(_q(TEXT, "p")):
                    text = self.text_of(sub)
                    if text:
                        parts.append(text)
        text = " ".join(" ".join(parts).split())
        if text:
            return text
        # No displayed text: the stored value, formatted by what it is.
        kind = cell.get(_q(OFFICE, "value-type"))
        if kind in ("float", "percentage", "currency"):
            value = cell.get(_q(OFFICE, "value"))
            if value is not None:
                try:
                    number = float(value)
                except ValueError:
                    return value
                if kind == "percentage":
                    return f"{round(number * 100, 6):g}%"
                return spreadsheet.cell_text(number)
        if kind == "date":
            return cell.get(_q(OFFICE, "date-value")) or ""
        if kind == "time":
            return cell.get(_q(OFFICE, "time-value")) or ""
        if kind == "boolean":
            return (
                "TRUE" if cell.get(_q(OFFICE, "boolean-value")) == "true" else "FALSE"
            )
        return ""


def _is_label(text: str) -> bool:
    """A cell worth filling down a rowspan: words, not a number."""
    return sum(1 for char in text if char.isalpha()) >= 3


def _check(file_path: str, label: str) -> dict:
    return ooxml.check_expansion(file_path, label)


# -- text ------------------------------------------------------------------------


def extract_text(file_path: str, *, image_ocr=None, max_images: int = 0, describe=None):
    """(pages, metadata) for an .odt: one page (the format has no pages)."""
    expansion = _check(file_path, "ODT")
    root = _content(file_path)
    body = root.find(f"{{{OFFICE}}}body")
    text_root = body.find(f"{{{OFFICE}}}text") if body is not None else None
    if text_root is None:
        raise StageError(
            "The OpenDocument file holds no text document.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )

    pictures = PictureReader(image_ocr, max_images, describe)
    reader = _Reader(file_path, pictures)
    builder = compose.PageBuilder(source=SOURCE, source_version=VERSION)
    try:
        reader.run(text_root, builder)
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"ODT text extraction failed: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    text, tables = builder.build()
    page = OdfPage(text=text, tables=tables)
    return [page], {
        "extractor": "odf",
        "extractor_version": VERSION,
        "paragraphs": reader.paragraphs,
        "tables": reader.tables,
        "charts": reader.charts,
        "text_boxes": reader.text_boxes,
        "pagination": "none",
        "pictures_ocred": pictures.read,
        "pictures_described": pictures.described,
        **expansion,
    }


# -- spreadsheets ----------------------------------------------------------------


def extract_sheets(file_path: str):
    """(pages, metadata) for an .ods, shaped as `spreadsheet.extract_xlsx`."""
    expansion = _check(file_path, "ODS")
    root = _content(file_path)
    body = root.find(f"{{{OFFICE}}}body")
    sheets_root = body.find(f"{{{OFFICE}}}spreadsheet") if body is not None else None
    if sheets_root is None:
        raise StageError(
            "The OpenDocument file holds no spreadsheet.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )

    reader = _Reader(file_path, PictureReader())
    budget = spreadsheet._Budget()
    pages: list = []
    sheets = hidden = total_rows = 0
    try:
        for table in sheets_root.iter(_q(TABLE, "table")):
            if table.getparent() is not sheets_root:
                continue
            if table.get(_q(TABLE, "display")) == "false":
                hidden += 1
                continue
            if budget.truncated:
                break
            rows = _sheet_rows(reader, table, budget)
            if not rows:
                continue
            sheets += 1
            total_rows += len(rows)
            name = table.get(_q(TABLE, "name")) or f"Sheet{sheets}"
            pages += spreadsheet.build_pages(
                rows,
                name=name,
                source="odf:sheet",
                source_version=VERSION,
                metadata={"sheet": name, "extractor": "odf"},
            )
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"ODS text extraction failed: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    if budget.truncated and pages:
        pages[-1] = spreadsheet._with_truncation_note(pages[-1])

    return pages, {
        "extractor": "odf",
        "extractor_version": VERSION,
        "sheets": sheets,
        "sheets_hidden": hidden,
        "rows": total_rows,
        "cells": budget.cells,
        "truncated": budget.truncated,
        **expansion,
    }


def _sheet_rows(reader: _Reader, table, budget) -> list:
    rows: list = []
    blank_run = 0
    for row in reader._rows(table):
        repeat = _count(row, "number-rows-repeated", MAX_REPEAT)
        values = []
        for cell in row:
            if local(cell) not in ("table-cell", "covered-table-cell"):
                continue
            text = reader.cell_text(cell) if local(cell) == "table-cell" else ""
            values.extend([text] * _count(cell, "number-columns-repeated"))
        values = values[: spreadsheet.MAX_COLUMNS]
        filled = sum(1 for value in values if value)
        if not filled:
            blank_run += repeat
            if blank_run > spreadsheet.MAX_BLANK_RUN:
                break
            rows.extend([[]] * repeat)
            continue
        blank_run = 0
        for _ in range(repeat):
            rows.append(list(values))
            if not budget.spend(filled):
                break
        if budget.truncated:
            break
    while rows and not any(rows[-1]):
        rows.pop()
    while rows and not any(rows[0]):
        rows.pop(0)
    return rows


# -- presentations ---------------------------------------------------------------


def slide_count(file_path: str) -> int:
    root = _content(file_path)
    body = root.find(f"{{{OFFICE}}}body")
    deck = body.find(f"{{{OFFICE}}}presentation") if body is not None else None
    return len(list(deck.iter(_q(DRAW, "page")))) if deck is not None else 0


def extract_slides(
    file_path: str, *, image_ocr=None, max_images: int = 0, describe=None
):
    """(pages, metadata) for an .odp: one page per slide."""
    expansion = _check(file_path, "ODP")
    root = _content(file_path)
    body = root.find(f"{{{OFFICE}}}body")
    deck = body.find(f"{{{OFFICE}}}presentation") if body is not None else None
    if deck is None:
        raise StageError(
            "The OpenDocument file holds no presentation.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )

    pictures = PictureReader(image_ocr, max_images, describe)
    reader = _Reader(file_path, pictures)
    pages: list = []
    notes = 0
    try:
        for number, slide in enumerate(deck.iter(_q(DRAW, "page")), start=1):
            builder = compose.PageBuilder(source=SOURCE, source_version=VERSION)
            title_first = sorted(
                (c for c in slide if local(c) in ("frame", "custom-shape")),
                key=_reading_rank,
            )
            for frame in title_first:
                _slide_frame(reader, frame, builder)
            note_text = ""
            for note in slide.iter(_q(PRESENTATION, "notes")):
                lines = [reader.text_of(p) for p in note.iter(_q(TEXT, "p"))]
                note_text = "\n".join(line for line in lines if line)
            if note_text:
                notes += 1
                builder.add_text(f"Notes: {note_text}")
            text, tables = builder.build()
            pages.append(
                OdfPage(
                    text=text,
                    tables=tables,
                    metadata={"slide": number, "extractor": "odf"},
                )
            )
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"ODP text extraction failed: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    return pages, {
        "extractor": "odf",
        "extractor_version": VERSION,
        "slides": len(pages),
        "tables": reader.tables,
        "charts": reader.charts,
        "notes": notes,
        "pictures_ocred": pictures.read,
        "pictures_described": pictures.described,
        **expansion,
    }


def _reading_rank(frame) -> int:
    """Title first, its subtitle next, then the rest in the order drawn."""
    kind = frame.get(_q(PRESENTATION, "class"))
    return {"title": 0, "subtitle": 1}.get(kind, 2)


def _slide_frame(reader: _Reader, frame, builder) -> None:
    for box in frame.iter(_q(DRAW, "text-box")):
        reader.run(box, builder)
    for obj in frame.iter(_q(DRAW, "object")):
        reader._chart(obj, builder)
    for image in frame.iter(_q(DRAW, "image")):
        reader._picture(image, builder)
    for table in frame.iter(_q(TABLE, "table")):
        reader._table(table, builder)
