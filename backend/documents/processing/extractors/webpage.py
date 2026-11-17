"""Web pages: HTML as a document, with its tables as tables.

A saved page, an exported report, a scraped notice - HTML is the format a
great deal of institutional text lives in, and read as plain text it is
mostly tags. The page is parsed with lxml (which copes with the broken markup
real pages have), and what is read is what a person SEES:

* headings, paragraphs, list items and preformatted blocks, one per line, in
  document order, with link text but not link targets;
* tables as tables - colspan and rowspan expanded so every cell is under its
  column, a header row where the markup says so - through the same builder
  every other format uses;
* the alternative text of pictures, which is how a page describes a figure
  it cannot show in words;
* nothing that is not content: scripts, styles, the document head, hidden
  elements, navigation and cookie banners that say they are what they are.

Nothing is fetched. A page is never rendered, no script runs, and the parser
neither loads external resources nor expands entities.
"""

import re

from lxml import etree, html

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose

SOURCE = "lxml:html"

# What a page is cut to: a scraped site dump can be tens of megabytes of
# markup for a few pages of words.
MAX_ELEMENTS = 400_000
MAX_TABLE_CELLS = 200_000

# Elements that carry no content, however much text is inside them.
SKIPPED = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "head",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "audio",
        "video",
        "map",
        "button",
        "select",
        "option",
        "datalist",
        "input",
        "textarea",
        "nav",
    }
)

# One line each.
BLOCKS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "aside",
        "header",
        "footer",
        "blockquote",
        "figure",
        "figcaption",
        "address",
        "details",
        "summary",
        "dl",
        "dt",
        "dd",
        "form",
        "fieldset",
        "legend",
        "caption",
        "center",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "pre",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)

HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_SPACE = re.compile(r"[ \t\r\n\f ]+")
# Class or id names that mark furniture rather than content.
_FURNITURE = re.compile(
    r"(^|[\s_-])(cookie|cookies|consent|gdpr|breadcrumb|breadcrumbs|"
    r"skip-link|sr-only|visually-hidden)([\s_-]|$)",
    re.IGNORECASE,
)


def _tag(element) -> str:
    tag = element.tag
    return tag.lower() if isinstance(tag, str) else ""


def _hidden(element) -> bool:
    if element.get("hidden") is not None:
        return True
    if (element.get("aria-hidden") or "").lower() == "true":
        return True
    if _HIDDEN.search(element.get("style") or ""):
        return True
    name = f"{element.get('class') or ''} {element.get('id') or ''}"
    return bool(_FURNITURE.search(name))


def _collapse(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


class _Reader:
    def __init__(self):
        self.builder = compose.PageBuilder(source=SOURCE, source_version=_version())
        self.elements = 0
        self.tables = 0
        self.truncated = False
        self.title = ""
        self.cells = 0

    # -- inline text ---------------------------------------------------

    def _inline(self, element, out: list) -> None:
        """The text of an element and its inline children, in `out`."""
        tag = _tag(element)
        if tag in SKIPPED or _hidden(element):
            return
        if tag == "br":
            out.append("\n")
            return
        if tag == "img":
            alt = _collapse(element.get("alt") or "")
            if alt:
                out.append(f" {alt} ")
            return
        if element.text:
            out.append(element.text)
        for child in element:
            self.elements += 1
            if self.elements > MAX_ELEMENTS:
                self.truncated = True
                return
            if _tag(child) in BLOCKS and _tag(child) not in HEADINGS:
                out.append("\n")
                self._inline(child, out)
                out.append("\n")
            else:
                self._inline(child, out)
            if child.tail:
                out.append(child.tail)

    def _text(self, element, *, keep_lines: bool = False) -> str:
        pieces: list = []
        self._inline(element, pieces)
        raw = "".join(pieces)
        if keep_lines:
            return raw.strip("\n")
        lines = [_collapse(line) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)

    # -- blocks --------------------------------------------------------

    def walk(self, element, list_stack: list) -> None:
        if self.truncated:
            return
        tag = _tag(element)
        if tag in SKIPPED or _hidden(element):
            return
        self.elements += 1
        if self.elements > MAX_ELEMENTS:
            self.truncated = True
            return

        if tag == "table":
            self._table(element)
            return
        if tag in HEADINGS:
            text = self._text(element)
            if text:
                self.builder.add_text(text)
            return
        if tag == "pre":
            text = self._text(element, keep_lines=True)
            if text.strip():
                self.builder.add_text(text)
            return
        if tag in ("ul", "ol"):
            self._list(element, tag, list_stack)
            return
        if tag == "hr":
            return

        if tag in ("p", "li", "dt", "dd", "caption", "figcaption", "summary", "legend"):
            if not self._has_block_child(element):
                text = self._text(element)
                if text:
                    self.builder.add_text(text)
                return

        # A container: its own text and its children, in order. Text that sits
        # directly in a div, between its block children, is a paragraph too.
        run: list = []

        def flush():
            text = _collapse("".join(run))
            run.clear()
            if text:
                self.builder.add_text(text)

        if element.text:
            run.append(element.text)
        for child in element:
            child_tag = _tag(child)
            if child_tag in BLOCKS or child_tag in ("table", "pre", "ul", "ol"):
                flush()
                self.walk(child, list_stack)
            else:
                self._inline(child, run)
            if child.tail:
                run.append(child.tail)
        flush()

    @staticmethod
    def _has_block_child(element) -> bool:
        return any(
            _tag(child) in ("table", "pre", "ul", "ol", "div", "p", "blockquote")
            for child in element
        )

    def _list(self, element, tag: str, stack: list) -> None:
        number = 0
        try:
            number = int(element.get("start") or 1) - 1
        except ValueError:
            number = 0
        depth = len(stack)
        for item in element:
            if _tag(item) != "li" or _hidden(item):
                continue
            number += 1
            marker = f"{number}." if tag == "ol" else "-"
            indent = "  " * depth
            nested = [c for c in item if _tag(c) in ("ul", "ol")]
            # The item's own text, without its nested lists.
            clone = etree.fromstring(etree.tostring(item))
            for child in list(clone):
                if _tag(child) in ("ul", "ol"):
                    clone.remove(child)
                    # keep what followed the nested list
                    if child.tail:
                        previous = child.getprevious()
                        if previous is not None:
                            previous.tail = (previous.tail or "") + child.tail
                        else:
                            clone.text = (clone.text or "") + child.tail
            text = self._text(clone)
            if text:
                self.builder.add_text(f"{indent}{marker} {text}".rstrip())
            for sub in nested:
                self._list(sub, _tag(sub), [*stack, item])

    # -- tables --------------------------------------------------------

    def _table(self, table) -> None:
        rows = self._rows(table)
        if not rows:
            return
        self.tables += 1
        caption = table.find(".//caption")
        if caption is not None:
            text = self._text(caption)
            if text:
                self.builder.add_text(text)
        header = self._has_header(table, rows)
        self.builder.add_table(rows, header=header)

    @staticmethod
    def _has_header(table, rows) -> bool:
        first = table.find(".//tr")
        if first is None:
            return False
        cells = [c for c in first if _tag(c) in ("th", "td")]
        return bool(cells) and all(_tag(c) == "th" for c in cells)

    def _rows(self, table) -> list:
        """The grid, with its spans made to line up.

        A cell that spans columns keeps its text in its first column and leaves
        the others blank. A cell that spans rows has its text filled down into
        the rows below when it is a LABEL (words: "Hostel", "Semester 1") and
        reserves its column blank when it is not (a number copied into the
        rows below would be read as a figure of theirs).
        """
        trs = [tr for tr in table.iter("tr") if self._owner(tr) is table]
        grid: list = []
        carry: dict = {}  # column -> [label or "", rows still to fill]
        for tr in trs:
            row: list = []
            for cell in tr:
                if _tag(cell) not in ("td", "th") or _hidden(cell):
                    continue
                self.cells += 1
                if self.cells > MAX_TABLE_CELLS:
                    self.truncated = True
                    break
                _carry_into(row, carry)
                text = self._cell(cell)
                span = _span(cell.get("colspan"))
                down = _span(cell.get("rowspan"))
                column = len(row)
                row.append(text)
                row.extend([""] * (span - 1))
                if down > 1:
                    carry[column] = [text if _is_label(text) else "", down - 1]
            _carry_into(row, carry)
            if any(cell.strip() for cell in row):
                grid.append(row)
        return grid

    @staticmethod
    def _owner(tr):
        """The table a row belongs to: a nested table's rows are its own."""
        node = tr.getparent()
        while node is not None and _tag(node) != "table":
            node = node.getparent()
        return node

    def _cell(self, cell) -> str:
        # A table inside a cell is flattened into the cell's text.
        return " ".join(self._text(cell).split("\n"))


def _carry_into(row: list, carry: dict) -> None:
    """Fill the columns a cell above spans down into, up to the next free one."""
    while len(row) in carry:
        text, left = carry[len(row)]
        row.append(text)
        if left <= 1:
            del carry[len(row) - 1]
        else:
            carry[len(row) - 1][1] = left - 1


def _is_label(text: str) -> bool:
    """A cell worth filling down a rowspan: words, not a number."""
    return sum(1 for char in text if char.isalpha()) >= 3


def _span(value) -> int:
    try:
        return max(1, min(int(value), 50))
    except (TypeError, ValueError):
        return 1


def _version() -> str:
    return etree.__version__


def extract_page(file_path: str) -> tuple:
    """(page, metadata): the page's text and tables, as a `WebPage`."""
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    try:
        parser = html.HTMLParser(
            recover=True, remove_comments=True, no_network=True, huge_tree=False
        )
        root = html.document_fromstring(raw, parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        raise StageError(
            f"Could not read the page: {exc}", code=ErrorCode.CORRUPT_DOCUMENT
        ) from exc

    reader = _Reader()
    title_element = root.find(".//title")
    if title_element is not None:
        reader.title = _collapse(title_element.text_content())

    body = root.find("body")
    if body is None:
        body = root

    # A page's title is what it is called, and the one line a search for the
    # page needs - unless the page's own first heading already says it.
    first_heading = root.find(".//h1")
    heading = (
        _collapse(first_heading.text_content()) if first_heading is not None else ""
    )
    if reader.title and reader.title.lower() != heading.lower():
        reader.builder.add_text(reader.title)
    reader.walk(body, [])

    text, tables = reader.builder.build()
    metadata = {
        "extractor": "lxml",
        "extractor_version": _version(),
        "tables": reader.tables,
        "truncated": reader.truncated,
    }
    if reader.title:
        metadata["title"] = reader.title[:300]
    return WebPage(text=text, tables=tables), metadata


class WebPage:
    def __init__(self, text: str = "", tables: list | None = None):
        self.text = text
        self.tables = tables or []
