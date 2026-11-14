"""Compose one page's text and the tables inside it, with exact spans.

Every extractor that can see real cells - a Word table, a ruled PDF grid, a
spreadsheet sheet, a slide table - writes the page through a `PageBuilder`
instead of joining strings itself. Two reasons, both about trust.

*The span is measured, not estimated.* A table is stored with the offsets of
its rows in the page text, and every cell must re-slice out of that span
(`tables.detector.verify`). If an extractor computed those offsets on one
string and stored another - an extra newline here, a stripped NUL there -
every span after the first would point a few characters off, and the
verification that exists to catch invented cells would reject real ones.
The builder owns the final string, so the offsets are taken from the very
characters that get stored.

*Every source reads the same.* A table is written as one ` | `-separated row
per line, whatever it came from, with blank cells left blank. A reader (or
the answerer) sees the same shape for a PDF, a DOCX and a spreadsheet, and
the chunker keeps it together as a table either way.

The cleanup is deliberately small and lossless in meaning: ligatures that a
PDF font encodes as one glyph are spelled out (so "office" is searchable),
soft hyphens and zero-width spaces are dropped, no-break spaces become
spaces. Zero-width JOINERS are kept - Indic scripts need them to spell
conjuncts, and removing them would corrupt the text rather than clean it.
"""

import re

from documents.processing.context import PageTable
from documents.processing.normalize import strip_nul
from documents.processing.tables import detector

# The separator written between cells. Matches `detector.render` and the
# PIPE separator the text detector and the answerer already understand.
CELL_SEPARATOR = " | "

# A literal pipe inside a cell would be read as a column boundary by anything
# that re-parses the row. The broken bar is the same glyph to a reader.
PIPE_IN_CELL = "\u00a6"

MIN_ROWS = detector.MIN_ROWS
MIN_COLUMNS = detector.MIN_COLUMNS

_TRANSLATE = {
    ord("\u00ad"): None,  # soft hyphen
    ord("\u200b"): None,  # zero-width space
    ord("\ufeff"): None,  # byte-order mark / zero-width no-break space
    ord("\u00a0"): " ",  # no-break space
    ord("\u202f"): " ",  # narrow no-break space
    ord("\u2007"): " ",  # figure space
    ord("\ufb00"): "ff",
    ord("\ufb01"): "fi",
    ord("\ufb02"): "fl",
    ord("\ufb03"): "ffi",
    ord("\ufb04"): "ffl",
    ord("\ufb05"): "st",
    ord("\ufb06"): "st",
}

# C0 and C1 controls other than tab and newline. A form feed from a PDF
# text layer or a stray BEL is never content.
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BLANK_RUNS = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """Normalize characters, never words: nothing here changes what is said."""
    if not text:
        return ""
    text = strip_nul(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_TRANSLATE)
    return _CONTROLS.sub("", text)


def clean_cell(text) -> str:
    """One cell as it is written into a row: a single line, no pipes."""
    if text is None:
        return ""
    flat = " ".join(clean_text(str(text)).split())
    return flat.replace("|", PIPE_IN_CELL)


def _grid(rows) -> list:
    """Cleaned, rectangular, with empty rows and empty columns removed."""
    cleaned = [[clean_cell(cell) for cell in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return []

    width = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]

    keep = [column for column in range(width) if any(row[column] for row in cleaned)]
    return [[row[column] for column in keep] for row in cleaned]


def _row_text(cells: list) -> str:
    return CELL_SEPARATOR.join(cells).strip()


class PageBuilder:
    """Accumulates blocks of text and tables for one page.

    >>> page = PageBuilder(source="demo")
    >>> page.add_text("Fee structure")
    >>> page.add_table([["Programme", "Fee"], ["CSE", "85000"]])
    True
    >>> text, tables = page.build()
    >>> text[tables[0].character_start:tables[0].character_end]
    'Programme | Fee\\nCSE | 85000'
    """

    def __init__(self, source: str = "", source_version: str = ""):
        self.source = source
        self.source_version = source_version
        self._pieces: list[str] = []
        self._length = 0
        self._last_was_table = False
        self._tables: list[PageTable] = []

    # -- adding ----------------------------------------------------------

    def _push(self, text: str, *, is_table: bool) -> int:
        """Append a block and return the offset it starts at."""
        if self._pieces:
            # A blank line sets a table apart from the prose around it, so
            # a run of text rows can never be read as part of a neighbouring
            # table; between two paragraphs a newline is enough.
            lead = "\n\n" if (is_table or self._last_was_table) else "\n"
            self._pieces.append(lead)
            self._length += len(lead)

        start = self._length
        self._pieces.append(text)
        self._length += len(text)
        self._last_was_table = is_table
        return start

    def add_text(self, text: str) -> None:
        """A paragraph, line, or block of lines. Empty text is ignored."""
        text = clean_text(text)
        lines = [line.rstrip() for line in text.split("\n")]
        text = _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip("\n")
        if not text.strip():
            return
        self._push(text, is_table=False)

    def add_table(
        self,
        rows,
        *,
        header="auto",
        source: str | None = None,
        source_version: str | None = None,
    ) -> bool:
        """Write a table and record where it landed.

        `header`: "auto" (the same narrow guess the text detector makes),
        True (the first row is the header), or False (none).

        Returns whether it was stored as a table. A grid with one row or
        one column is not one - it is a label, or a list - so it is written
        as plain text instead and returns False. Nothing is dropped.
        """
        grid = _grid(rows)
        if not grid:
            return False

        if len(grid) < MIN_ROWS or len(grid[0]) < MIN_COLUMNS:
            self._add_degenerate(rows, grid)
            return False

        text = "\n".join(_row_text(row) for row in grid)
        start = self._push(text, is_table=True)

        if header == "auto":
            head = detector.guess_header(grid)
        elif header:
            head = list(grid[0])
        else:
            head = []

        self._tables.append(
            PageTable(
                character_start=start,
                character_end=start + len(text),
                rows=grid,
                header=head,
                source=source if source is not None else self.source,
                source_version=(
                    source_version
                    if source_version is not None
                    else self.source_version
                ),
            )
        )
        return True

    def _add_degenerate(self, rows, grid) -> None:
        if len(grid) == 1 and len(grid[0]) > 1:
            # One row: a signature line, a label and its value.
            self.add_text(_row_text(grid[0]))
            return
        # One column (or one cell): a boxed paragraph or a list. Each cell
        # keeps its own line breaks, since the cell was prose before it was
        # a table.
        for row in rows:
            for cell in row:
                if cell is not None:
                    self.add_text(str(cell))

    # -- reading ---------------------------------------------------------

    @property
    def text(self) -> str:
        return "".join(self._pieces)

    @property
    def tables(self) -> list:
        return list(self._tables)

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip())

    def build(self) -> tuple:
        """(page text, tables). The text has no leading or trailing blank."""
        return self.text, self.tables
