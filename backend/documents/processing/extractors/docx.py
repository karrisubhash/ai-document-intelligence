"""DOCX text extraction.

Two things about this format need stating, because both are easy to get
quietly wrong.

*A DOCX has no page count.* Pagination in Word is a rendering result - it
depends on the fonts, page size and printer metrics of whoever opens the
file - so there is no page number to read out of the bytes. What the file
does record is where the author asked for a page break (a break, "page break
before" on a paragraph, a section that starts on a new page). This extractor
splits on those and labels the result `explicit_breaks` in metadata, so a
reader can tell an author-declared boundary from a rendered one. A document
with no breaks is one page, labelled `none`. Claiming anything more precise
would be inventing structure the file does not contain.

*A DOCX is a ZIP archive*, and ZIP archives from untrusted users can be
decompression bombs. See `ooxml`: the uncompressed total is checked before
anything is parsed.

The body is walked as XML, in document order, rather than through
python-docx's `paragraphs` and `tables`, because those accessors silently
skip most of what carries real content in an institutional document:

* content controls (`w:sdt`) - the fill-in fields of a form or template,
* tracked insertions (`w:ins`), which are not direct children of a paragraph,
* text boxes (`w:txbxContent`), where letterheads and notices live,
* nested tables, and the cells of a merged table - `row.cells` repeats a
  merged cell's text once per grid column it spans,
* list numbers. "2.3.1" is not in the paragraph's text; it is generated from
  numbering.xml. A clause numbered by Word has to be numbered here too, or
  "see clause 4.2" refers to nothing the reader can find,
* headers, footers, footnotes and endnotes, which are text the document says.

Tables are written through `PageBuilder` as structure (rows of cells, blank
cells kept), so the Tables tab and the answerer see real columns.
"""

import re
from dataclasses import dataclass, field

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import charts, compose, ooxml
from documents.processing.extractors.listmarks import format_number
from documents.processing.extractors.ooxml import local
from documents.processing.extractors.pictures import PictureReader

# The part every real Word document has. Its presence is what separates a
# .docx from the other ZIP-based office formats (.xlsx, .pptx, .odt), which
# share the same magic bytes and would otherwise be handed to this extractor.
DOCUMENT_PART = "word/document.xml"

MAX_UNCOMPRESSED_BYTES = ooxml.MAX_UNCOMPRESSED_BYTES
MAX_COMPRESSION_RATIO = ooxml.MAX_COMPRESSION_RATIO

VERSION = ""
SOURCE = "python-docx:table"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
V = "urn:schemas-microsoft-com:vml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# Section breaks that do NOT start a new page.
CONTINUOUS = "continuous"

NOTE_TYPES_SKIPPED = {"separator", "continuationSeparator", "continuationNotice"}


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


def _val(element) -> str | None:
    return None if element is None else element.get(_w("val"))


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
    """Whether this ZIP is specifically a Word document."""
    return ooxml.has_part(file_path, DOCUMENT_PART)


def check_expansion(file_path: str) -> dict:
    """Reject decompression bombs before handing the file to a parser."""
    return ooxml.check_expansion(file_path, "DOCX")


# -- numbering --------------------------------------------------------------


@dataclass
class _Level:
    start: int = 1
    fmt: str = "decimal"
    text: str = ""


class _Numbering:
    """Generates the list numbers Word stores as a recipe, not as text."""

    def __init__(self, document):
        self.levels: dict = {}  # abstractNumId -> {ilvl: _Level}
        self.nums: dict = {}  # numId -> abstractNumId
        self.overrides: dict = {}  # (numId, ilvl) -> start
        self.style_numbering: dict = {}  # styleId -> (numId, ilvl) | None
        self.style_parent: dict = {}
        self.counts: dict = {}  # numId -> {ilvl: count}

        try:
            numbering = document.part.numbering_part.element
        except Exception:
            numbering = None

        if numbering is not None:
            self._read_numbering(numbering)

        try:
            styles = document.styles.element
        except Exception:
            styles = None

        if styles is not None:
            self._read_styles(styles)

    def _read_numbering(self, numbering) -> None:
        for abstract in numbering.findall(_w("abstractNum")):
            levels = {}
            for level in abstract.findall(_w("lvl")):
                try:
                    index = int(level.get(_w("ilvl"), "0"))
                    start = int(_val(level.find(_w("start"))) or 1)
                except ValueError:
                    continue
                levels[index] = _Level(
                    start=start,
                    fmt=_val(level.find(_w("numFmt"))) or "decimal",
                    text=_val(level.find(_w("lvlText"))) or "",
                )
            self.levels[abstract.get(_w("abstractNumId"))] = levels

        for num in numbering.findall(_w("num")):
            num_id = num.get(_w("numId"))
            self.nums[num_id] = _val(num.find(_w("abstractNumId")))
            for override in num.findall(_w("lvlOverride")):
                start = override.find(_w("startOverride"))
                if start is not None and _val(start) is not None:
                    try:
                        key = (num_id, int(override.get(_w("ilvl"), "0")))
                        self.overrides[key] = int(_val(start))
                    except ValueError:
                        pass

    def _read_styles(self, styles) -> None:
        for style in styles.findall(_w("style")):
            style_id = style.get(_w("styleId"))
            self.style_parent[style_id] = _val(style.find(_w("basedOn")))
            properties = style.find(_w("pPr"))
            numbering = None if properties is None else properties.find(_w("numPr"))
            if numbering is not None:
                self.style_numbering[style_id] = self._read_numpr(numbering)

    @staticmethod
    def _read_numpr(numpr):
        num_id = _val(numpr.find(_w("numId")))
        try:
            ilvl = int(_val(numpr.find(_w("ilvl"))) or 0)
        except ValueError:
            ilvl = 0
        return num_id, ilvl

    def _of_style(self, style_id):
        for _ in range(8):  # a basedOn chain is short; bounded against cycles
            if style_id is None:
                return None
            if style_id in self.style_numbering:
                return self.style_numbering[style_id]
            style_id = self.style_parent.get(style_id)
        return None

    def reference(self, paragraph):
        """(numId, ilvl) for a numbered paragraph, else None."""
        properties = paragraph.find(_w("pPr"))
        if properties is None:
            return None

        numpr = properties.find(_w("numPr"))
        if numpr is not None:
            num_id, ilvl = self._read_numpr(numpr)
            # numId 0 is Word's way of saying "not numbered", even when the
            # style says it is.
            return None if num_id in (None, "0") else (num_id, ilvl)

        found = self._of_style(_val(properties.find(_w("pStyle"))))
        if found is None or found[0] in (None, "0"):
            return None
        return found

    def label(self, paragraph) -> tuple:
        """(label, indent level) - ("", 0) for an unnumbered paragraph.

        Called for every numbered paragraph, empty or not: Word counts an
        empty numbered paragraph, so skipping it would shift every number
        after it.
        """
        reference = self.reference(paragraph)
        if reference is None:
            return "", 0

        num_id, ilvl = reference
        levels = self.levels.get(self.nums.get(num_id))
        if not levels or ilvl not in levels:
            return "", 0

        level = levels[ilvl]
        counts = self.counts.setdefault(num_id, {})
        start = self.overrides.get((num_id, ilvl), level.start)
        counts[ilvl] = counts.get(ilvl, start - 1) + 1
        for deeper in [index for index in counts if index > ilvl]:
            del counts[deeper]

        if level.fmt == "bullet":
            return self._bullet(level.text), ilvl
        if level.fmt == "none":
            return "", ilvl

        def fill(match):
            index = int(match.group(1)) - 1
            other = levels.get(index, _Level())
            value = counts.get(index, self.overrides.get((num_id, index), other.start))
            return format_number(value, other.fmt)

        template = level.text or f"%{ilvl + 1}."
        return re.sub(r"%(\d)", fill, template).strip(), ilvl

    @staticmethod
    def _bullet(text: str) -> str:
        # Word stores a bullet as one glyph from a symbol font - a private-use
        # code point that means nothing outside that font.
        if text in ("-", "*", "+") or text in (chr(0x2013), chr(0x2014)):
            return text
        return chr(0x2022)


# -- the walk ---------------------------------------------------------------


class _PageBreak:
    pass


@dataclass
class _TextBox:
    element: object


@dataclass
class _Picture:
    rid: str


@dataclass
class _Chart:
    rid: str


@dataclass
class DocxPage:
    text: str = ""
    tables: list = field(default_factory=list)


class _Extractor:
    def __init__(self, document, file_path, image_ocr, max_images, describe=None):
        self.document = document
        self.file_path = file_path
        self.numbering = _Numbering(document)
        self.pictures = PictureReader(image_ocr, max_images, describe)

        self.pages: list = []
        self.builder = None
        # Whether the BODY has put anything on the current page. A header or
        # footer is on every page by definition and must not make a document
        # that opens with a page break grow a page of nothing but a header.
        self.has_body = False
        self.depth = 0
        self.paragraphs = 0
        self.tables = 0
        self.charts = 0
        self.text_boxes = 0
        self.footnote_numbers: dict = {}
        self.endnote_numbers: dict = {}

    # -- pages ---------------------------------------------------------

    def _new_page(self) -> None:
        self.builder = compose.PageBuilder(source=SOURCE, source_version=VERSION)
        self.pages.append(self.builder)
        self.has_body = False

    def _break_page(self) -> None:
        # Consecutive breaks, or a break on an empty page, make no new page:
        # an author's stray extra break is not a blank page in the record.
        if self.has_body:
            self._new_page()

    def _emit_text(self, text: str) -> None:
        self.builder.add_text(text)
        if text.strip():
            self.has_body = True

    def _emit_table(self, rows) -> None:
        self.builder.add_table(rows)
        self.has_body = True

    # -- blocks --------------------------------------------------------

    def _blocks(self, parent):
        for child in parent:
            name = local(child)
            if name in ("p", "tbl"):
                yield name, child
            elif name == "sdt":
                content = child.find(_w("sdtContent"))
                if content is not None:
                    yield from self._blocks(content)
            elif name in ("ins", "moveTo", "customXml", "smartTag"):
                yield from self._blocks(child)
            elif name == "AlternateContent":
                yield from self._blocks(self._choice(child))

    @staticmethod
    def _choice(alternate):
        """The branch Word itself would show: Choice, else Fallback.

        An AlternateContent holds the SAME content twice - once for newer
        Word, once for older - so reading both duplicates every text box.
        """
        choice = alternate.find(f"{{{MC}}}Choice")
        if choice is None:
            choice = alternate.find(f"{{{MC}}}Fallback")
        return choice if choice is not None else alternate

    def run_body(self, parent, allow_breaks=True) -> None:
        for kind, element in self._blocks(parent):
            if kind == "p":
                self._body_paragraph(element, allow_breaks)
            else:
                self._body_table(element)

    # -- paragraphs ----------------------------------------------------

    def _inline(self, element, out) -> None:
        for child in element:
            name = local(child)
            if name == "r":
                self._run(child, out)
            elif name in ("hyperlink", "ins", "moveTo", "smartTag", "fldSimple"):
                self._inline(child, out)
            elif name in ("sdt",):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    self._inline(content, out)
            elif name == "AlternateContent":
                self._inline(self._choice(child), out)
            elif name in ("oMath", "oMathPara"):
                formula = "".join(t.text or "" for t in child.iter(f"{{{M}}}t"))
                if formula.strip():
                    out.append(f" {formula.strip()} ")
            # w:del and w:moveFrom are deliberately absent: deleted text is
            # not part of the document as it now reads.

    def _run(self, run, out) -> None:
        for child in run:
            name = local(child)
            if name == "t":
                out.append(child.text or "")
            elif name == "tab":
                out.append("\t")
            elif name in ("br", "cr"):
                if name == "br" and child.get(_w("type")) == "page":
                    out.append(_PageBreak())
                else:
                    out.append("\n")
            elif name == "noBreakHyphen":
                out.append("-")
            elif name == "footnoteReference":
                out.append(self._note_marker(child, "fn", self.footnote_numbers))
            elif name == "endnoteReference":
                out.append(self._note_marker(child, "en", self.endnote_numbers))
            elif name in ("drawing", "pict", "object"):
                self._graphic(child, out)
            elif name == "AlternateContent":
                for inner in self._choice(child):
                    if local(inner) in ("drawing", "pict", "object"):
                        self._graphic(inner, out)

    @staticmethod
    def _note_marker(reference, prefix, numbers) -> str:
        note_id = reference.get(_w("id"))
        number = numbers.get(note_id)
        return f"[{prefix}{number}]" if number else ""

    def _graphic(self, element, out) -> None:
        for box in element.iter(_w("txbxContent")):
            out.append(_TextBox(box))
        for chart in element.iter(f"{{{CHART}}}chart"):
            rid = chart.get(f"{{{R}}}id")
            if rid:
                out.append(_Chart(rid))
        for blip in element.iter(f"{{{A}}}blip"):
            rid = blip.get(f"{{{R}}}embed")
            if rid:
                out.append(_Picture(rid))
        for image in element.iter(f"{{{V}}}imagedata"):
            rid = image.get(f"{{{R}}}id")
            if rid:
                out.append(_Picture(rid))

    def _items(self, paragraph) -> list:
        out: list = []
        self._inline(paragraph, out)
        return out

    @staticmethod
    def _starts_new_page_before(paragraph) -> bool:
        properties = paragraph.find(_w("pPr"))
        if properties is None:
            return False
        before = properties.find(_w("pageBreakBefore"))
        # Present with no val (or val true) means yes; val 0/false means no.
        return before is not None and _val(before) not in ("0", "false", "off")

    @staticmethod
    def _ends_with_section_break(paragraph) -> bool:
        properties = paragraph.find(_w("pPr"))
        section = None if properties is None else properties.find(_w("sectPr"))
        if section is None:
            return False
        kind = _val(section.find(_w("type"))) or "nextPage"
        return kind != CONTINUOUS

    def _line(self, pieces, label, indent) -> str:
        text = "".join(pieces).strip()
        if label and text:
            return f"{'  ' * indent}{label} {text}"
        return text

    def _body_paragraph(self, paragraph, allow_breaks) -> None:
        if self.depth == 0:
            self.paragraphs += 1

        if allow_breaks and self._starts_new_page_before(paragraph):
            self._break_page()

        label, indent = self.numbering.label(paragraph)

        pieces: list = []
        trailing: list = []
        labelled = False

        def flush():
            nonlocal labelled
            line = self._line(pieces, "" if labelled else label, indent)
            if line:
                self._emit_text(line)
                labelled = True
            pieces.clear()

        for item in self._items(paragraph):
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, _PageBreak):
                flush()
                if allow_breaks:
                    self._break_page()
            else:
                trailing.append(item)

        flush()

        for item in trailing:
            if isinstance(item, _TextBox):
                self._text_box(item.element)
            elif isinstance(item, _Chart):
                self._chart(item.rid)
            else:
                self._picture(item.rid)

        if allow_breaks and self._ends_with_section_break(paragraph):
            self._break_page()

    def _text_box(self, element) -> None:
        self.text_boxes += 1
        self.depth += 1
        try:
            self.run_body(element, allow_breaks=False)
        finally:
            self.depth -= 1

    # -- charts --------------------------------------------------------

    def _chart(self, rid) -> None:
        """A chart as the table of numbers it was drawn from.

        Exact, and no model: the data is in the file (extractors/charts.py).
        """
        try:
            blob = self.document.part.related_parts[rid].blob
        except Exception:
            return
        chart = charts.read(blob)
        if not chart:
            return
        self.charts += 1
        heading = f"{chart['kind'].capitalize()}" + (
            f": {chart['title']}" if chart["title"] else ""
        )
        self._emit_text(heading)
        self.builder.add_table(chart["rows"], header=True, source=charts.SOURCE)
        self.has_body = True
        if chart["truncated"]:
            self._emit_text("(the chart has more points than are listed here)")

    # -- pictures ------------------------------------------------------

    def _picture(self, rid) -> None:
        try:
            blob = self.document.part.related_parts[rid].blob
        except Exception:
            return

        text = self.pictures.text(blob)
        if text:
            self._emit_text(text)

    # -- tables --------------------------------------------------------

    def _cell_text(self, cell) -> str:
        parts = []
        for kind, element in self._blocks(cell):
            if kind == "p":
                label, indent = self.numbering.label(element)
                pieces: list = []
                self._inline(element, pieces)
                text = "".join(p for p in pieces if isinstance(p, str))
                text = self._line([text], label, indent)
                boxes = [p for p in pieces if isinstance(p, _TextBox)]
                for box in boxes:
                    inner = self._plain(box.element)
                    if inner:
                        text = f"{text} {inner}".strip()
                if text:
                    parts.append(text)
            else:
                nested = self._table_rows(element)
                flat = "; ".join(" / ".join(c for c in row if c) for row in nested)
                if flat.strip():
                    parts.append(flat)
        return "\n".join(parts)

    def _table_rows(self, table) -> list:
        rows = []
        for row in table:
            if local(row) != "tr":
                continue

            cells = []
            properties = row.find(_w("trPr"))
            before = None if properties is None else properties.find(_w("gridBefore"))
            if before is not None:
                try:
                    cells.extend([""] * int(_val(before) or 0))
                except ValueError:
                    pass

            for cell in row:
                if local(cell) != "tc":
                    continue
                cell_properties = cell.find(_w("tcPr"))
                span = 1
                continuation = False
                if cell_properties is not None:
                    span_element = cell_properties.find(_w("gridSpan"))
                    if span_element is not None and _val(span_element):
                        try:
                            span = max(1, int(_val(span_element)))
                        except ValueError:
                            span = 1
                    merge = cell_properties.find(_w("vMerge"))
                    # A vMerge without val="restart" continues the cell above.
                    continuation = merge is not None and _val(merge) != "restart"

                text = "" if continuation else self._cell_text(cell)
                cells.append(text)
                cells.extend([""] * (span - 1))

            rows.append(cells)
        return rows

    def _body_table(self, table) -> None:
        if self.depth == 0:
            self.tables += 1
        self.depth += 1
        try:
            rows = self._table_rows(table)
        finally:
            self.depth -= 1
        self._emit_table(rows)

    # -- everything that is not the body -------------------------------

    def _plain(self, element) -> str:
        """The text of a block container, as a flat string, with no pages."""
        saved = (self.builder, self.pages, self.depth)
        self.builder = compose.PageBuilder()
        self.pages = [self.builder]
        self.depth = 1
        try:
            self.run_body(element, allow_breaks=False)
            return self.builder.text.strip()
        finally:
            self.builder, self.pages, self.depth = saved

    def headers_and_footers(self) -> tuple:
        headers: list = []
        footers: list = []
        seen: set = set()

        try:
            odd_even = bool(self.document.settings.odd_and_even_pages_header_footer)
        except Exception:
            odd_even = False

        for section in self.document.sections:
            wanted = [
                (section.header, headers),
                (section.footer, footers),
            ]
            if section.different_first_page_header_footer:
                wanted += [
                    (section.first_page_header, headers),
                    (section.first_page_footer, footers),
                ]
            if odd_even:
                wanted += [
                    (section.even_page_header, headers),
                    (section.even_page_footer, footers),
                ]

            for part, store in wanted:
                if part.is_linked_to_previous:
                    continue
                text = self._plain(part._element)
                key = " ".join(text.split())
                if key and key not in seen:
                    seen.add(key)
                    store.append(text)

        return headers, footers

    def _notes(self, part: str, tag: str, prefix: str, numbers: dict) -> list:
        root = ooxml.read_part(self.file_path, part)
        if root is None:
            return []

        notes = []
        for note in root.findall(_w(tag)):
            note_id = note.get(_w("id"))
            if note_id not in numbers:
                continue
            text = self._plain(note)
            if text:
                notes.append(f"[{prefix}{numbers[note_id]}] {text}")
        return notes

    def number_notes(self) -> None:
        """Assign footnote and endnote numbers before the body is read, so a
        reference in the text can print the marker its note will carry."""
        for part, tag, numbers in (
            ("word/footnotes.xml", "footnote", self.footnote_numbers),
            ("word/endnotes.xml", "endnote", self.endnote_numbers),
        ):
            root = ooxml.read_part(self.file_path, part)
            if root is None:
                continue
            for note in root.findall(_w(tag)):
                if note.get(_w("type")) not in NOTE_TYPES_SKIPPED:
                    numbers[note.get(_w("id"))] = len(numbers) + 1


def extract_page_results(
    file_path: str, *, image_ocr=None, max_images: int = 0, describe=None
):
    """(pages, metadata): `DocxPage`s with their tables as structure.

    `image_ocr(PIL.Image) -> OcrPage` reads a picture embedded in the body;
    `max_images` caps how many one document may spend on it. `describe(image)
    -> str` is a vision model's reading of a figure, tried before OCR.

    Page boundaries are the author's explicit breaks. See the module
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

    walker = _Extractor(document, file_path, image_ocr, max_images, describe)

    try:
        walker.number_notes()
        headers, footers = walker.headers_and_footers()

        walker._new_page()
        for text in headers:
            walker.builder.add_text(text)

        walker.run_body(document.element.body)

        notes = walker._notes(
            "word/footnotes.xml", "footnote", "fn", walker.footnote_numbers
        ) + walker._notes("word/endnotes.xml", "endnote", "en", walker.endnote_numbers)

        # Footers and notes close the document, on its last page.
        for text in footers + notes:
            walker.builder.add_text(text)
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"DOCX text extraction failed: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

    built = [builder.build() for builder in walker.pages]
    # A page break at the very end leaves an empty last page nobody wrote.
    while len(built) > 1 and not built[-1][0].strip():
        built.pop()

    pages = [DocxPage(text=text, tables=tables) for text, tables in built]

    metadata = {
        "extractor": "python-docx",
        "extractor_version": VERSION,
        "paragraphs": walker.paragraphs,
        "tables": walker.tables,
        "charts": walker.charts,
        "pagination": "explicit_breaks" if len(pages) > 1 else "none",
        "text_boxes": walker.text_boxes,
        "headers_footers": len(headers) + len(footers),
        "notes": len(notes),
        "pictures_ocred": walker.pictures.read,
        "pictures_described": walker.pictures.described,
        **expansion,
    }
    return pages, metadata


def extract_pages(file_path: str) -> tuple:
    """Returns (list of page texts, metadata)."""
    pages, metadata = extract_page_results(file_path)
    return [page.text for page in pages], metadata


def page_count(file_path: str) -> int:
    """Derived page count, for the VALIDATION stage's limit check.

    Requires a full parse, because the number does not exist until the
    breaks have been walked.
    """
    pages, _ = extract_pages(file_path)
    return len(pages)


__all__ = [
    "DOCUMENT_PART",
    "DocxPage",
    "check_expansion",
    "extract_page_results",
    "extract_pages",
    "is_docx",
    "page_count",
]
