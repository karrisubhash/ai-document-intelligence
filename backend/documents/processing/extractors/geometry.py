"""Reading order and borderless tables, from word boxes alone.

A PDF text layer and an OCR pass both end up as the same thing: words with
rectangles. Plain text extraction throws the rectangles away and reads the
words in the order they were drawn, which is how a fee table becomes
"Programme Tuition Exam Fee B.Tech CSE 85000 2500" and a two-column page
becomes alternating half-sentences. This module keeps the rectangles long
enough to answer two questions: *which words are on the same line*, and
*which vertical gaps run unbroken through a stack of lines*.

That second question is the whole idea. A run of lines that share a
whitespace corridor from top to bottom is laid out in columns - but columns
of what? Three things look like this and they must be told apart:

* a **table**: short cells, usually a column of numbers, one row per line;
* **page columns** of prose (a newsletter, a paper): two or three tall
  columns that should be read one after another, not row by row;
* a **hanging-indent list**: "1.1" in a narrow column and the clause beside
  it. It has a corridor too. It is neither of the above, and reading it by
  column would put every number before every clause - so it is left as the
  ordinary lines it is.

`_classify` makes that call, and errs toward leaving text alone. A table
that is missed is still readable text; a list read column-first, or prose
cut into cells, is damage.

Pure functions, no Django, in the coordinate system of whoever calls: points
for a PDF, pixels for OCR. Every threshold is relative to the page's own
median word height, so neither is special.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from statistics import mean, median

# A gap between two words at least this many median word-heights wide is a
# column gap rather than a space. A space is ~0.3 of a height, justified text
# stretches it to ~0.5; padding between table columns is well above 0.6.
GAP_FACTOR = 0.6

# Two words are on one line when they overlap vertically by at least this
# fraction of the shorter one. Half tolerates a superscript and a subscript
# and still keeps neighbouring lines apart.
LINE_OVERLAP = 0.5

# A vertical gap between consecutive lines bigger than this many heights ends
# a band; it is a different block of the page, not another row.
MAX_ROW_GAP = 2.0

# A line this close under the previous one (in heights) with an empty first
# cell is the continuation of a wrapped cell, not a new row.
CONTINUATION_GAP = 0.5

# A gap above this (in heights) between lines of text is a paragraph break.
PARAGRAPH_GAP = 0.6

# Bounds the quadratic worst case of retrying band starts on a pathological
# page; a real table or column band is far shorter.
MAX_BAND_LINES = 400

MIN_TABLE_ROWS = 3
MIN_COLUMN_LINES = 4

# What a list pulls into its own narrow column: "1.", "(a)", "iv)", a clause
# number like "2.3.4", or a bullet glyph.
_MARKER = re.compile(
    r"^(\(?[0-9A-Za-z]{1,3}[.)]"
    r"|\d{1,3}(\.\d{1,3})+\.?"
    r"|[•◦▪■●○·*\-–—])$"
)
_CURRENCY = re.compile(r"^(rs\.?|inr|usd|eur)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^\d[\d,.:/%\-–]*%?$")


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 0.0)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass
class Block:
    """One piece of a page in reading order: some text, or one table."""

    kind: str  # "text" | "table"
    top: float
    bottom: float
    text: str = ""
    rows: list = field(default_factory=list)
    # Where the block came from, for the caller's own bookkeeping.
    source: str = ""


@dataclass
class Line:
    words: list
    top: float
    bottom: float

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return max(word.x1 for word in self.words)

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


# -- lines ------------------------------------------------------------------


def group_lines(words) -> list:
    """Words into visual lines, top to bottom, left to right within a line."""
    ordered = sorted(words, key=lambda word: (word.center_y, word.x0))
    lines: list = []

    for word in ordered:
        if lines:
            line = lines[-1]
            overlap = min(line.bottom, word.bottom) - max(line.top, word.top)
            shorter = min(line.bottom - line.top, word.height) or 1.0
            if overlap >= LINE_OVERLAP * shorter:
                line.words.append(word)
                line.top = min(line.top, word.top)
                line.bottom = max(line.bottom, word.bottom)
                continue
        lines.append(Line([word], word.top, word.bottom))

    for line in lines:
        line.words.sort(key=lambda word: word.x0)

    return lines


def _segments(line: Line, gap_min: float) -> list:
    """The runs of words on a line, split wherever the gap is a column gap."""
    segments = []
    start = previous = None
    for word in line.words:
        if previous is not None and word.x0 - previous.x1 >= gap_min:
            segments.append((start.x0, previous.x1))
            start = None
        if start is None:
            start = word
        previous = word
    segments.append((start.x0, previous.x1))
    return segments


def _corridors_of(segments: list) -> list:
    return [(a[1], b[0]) for a, b in zip(segments, segments[1:], strict=False)]


def _narrow(corridors: list, words, gap_min: float) -> list:
    """What is left of each corridor once `words` are laid over it.

    A corridor keeps at most one piece - the widest. A word sitting in the
    middle of a wide gap (a centred heading, an overflowing cell) must not
    split it in two: that would invent a column the page does not have.
    """
    kept = []
    for left, right in corridors:
        pieces = [(left, right)]
        for word in words:
            if word.x1 <= left or word.x0 >= right:
                continue
            narrowed = []
            for a, b in pieces:
                if word.x1 <= a or word.x0 >= b:
                    narrowed.append((a, b))
                    continue
                if word.x0 > a:
                    narrowed.append((a, word.x0))
                if word.x1 < b:
                    narrowed.append((word.x1, b))
            pieces = narrowed
        pieces = [piece for piece in pieces if piece[1] - piece[0] >= gap_min]
        if pieces:
            kept.append(max(pieces, key=lambda piece: piece[1] - piece[0]))
    return kept


# -- bands ------------------------------------------------------------------


def _grow_band(lines: list, start: int, gap_min: float, height: float):
    """The longest run of lines from `start` that share a corridor.

    Returns (end, corridors) or None. The first line must itself be laid out
    in at least two segments; later lines may be a single segment so long as
    it stays inside one column (a wrapped cell), because a line that crosses
    a corridor - any full-width sentence - is exactly what ends the band.
    """
    first = _segments(lines[start], gap_min)
    if len(first) < 2:
        return None

    corridors = _corridors_of(first)
    end = start + 1

    while end < len(lines) and end - start < MAX_BAND_LINES:
        previous, line = lines[end - 1], lines[end]
        if line.top - previous.bottom > MAX_ROW_GAP * height:
            break
        narrowed = _narrow(corridors, line.words, gap_min)
        if not narrowed:
            break
        corridors = narrowed
        end += 1

    return end, corridors


def _bounds(corridors: list) -> list:
    return [(left + right) / 2 for left, right in corridors]


def _cells(line: Line, bounds: list) -> list:
    cells = [[] for _ in range(len(bounds) + 1)]
    for word in line.words:
        cells[bisect_right(bounds, word.center_x)].append(word.text)
    return [" ".join(cell) for cell in cells]


def _is_number(cell: str) -> bool:
    text = _CURRENCY.sub("", cell.strip().lstrip("₹$€£¥+-−("))
    text = text.rstrip(")").strip()
    return bool(text) and bool(_NUMBER.match(text.replace(" ", "")))


def _word_count(cell: str) -> int:
    return len(cell.split())


def _merge_continuations(lines: list, rows: list, height: float) -> list:
    """One row per table row: wrapped lines fold into the row above."""
    merged: list = []
    previous_line = None

    for line, cells in zip(lines, rows, strict=True):
        folds = (
            merged
            and not cells[0]
            and any(cells)
            and line.top - previous_line.bottom <= CONTINUATION_GAP * height
        )
        if folds:
            row = merged[-1]
            for index, cell in enumerate(cells):
                if cell:
                    row[index] = f"{row[index]} {cell}".strip()
        else:
            merged.append(list(cells))
        previous_line = line

    return merged


def _classify(raw_rows: list, merged_rows: list) -> str | None:
    """ "table", "columns" or None for a band, judged on what is in it.

    `merged_rows` has wrapped cells folded together; `raw_rows` is one row
    per line, which is what matters when asking whether the band is prose.
    """
    columns = len(merged_rows[0])
    if columns < 2:
        return None

    body = merged_rows[1:] if len(merged_rows) > 1 else merged_rows
    filled = [cell for row in merged_rows for cell in row if cell]
    if not filled:
        return None

    # A column of "1.", "(a)", bullets: a list with its markers pulled into
    # a corridor, not a table. Reading it row by row is the ordinary text.
    if columns == 2:
        firsts = [row[0] for row in merged_rows if row[0]]
        if firsts and sum(1 for c in firsts if _MARKER.match(c)) >= 0.8 * len(firsts):
            return None

    numeric_columns = 0
    for index in range(columns):
        cells = [row[index] for row in body if row[index]]
        needed = 1 if len(merged_rows) == 2 else 2
        if len(cells) >= needed and sum(map(_is_number, cells)) >= 0.6 * len(cells):
            numeric_columns += 1

    words = mean(_word_count(cell) for cell in filled)
    rows = len(merged_rows)

    if numeric_columns:
        # Numbers beside a paragraph of prose is a numbered clause, whatever
        # it looks like; a table's cells are short.
        return "table" if words <= 15 else None

    # A label beside its value - "Name | Asha Rao" - is a form, and a form
    # is a table. One short column is what sets it apart from prose columns,
    # whose cells are all a line of a sentence long.
    if rows >= MIN_TABLE_ROWS and columns == 2:
        counts = [
            mean(_word_count(row[index]) for row in merged_rows if row[index])
            for index in range(2)
            if any(row[index] for row in merged_rows)
        ]
        if len(counts) == 2 and min(counts) <= 3:
            return "table"

    # Every column a line of sentence, on nearly every line: page columns.
    # Checked before the generic "short cells" rule below, because narrow
    # newsletter columns are short too.
    if len(raw_rows) >= MIN_COLUMN_LINES and columns <= 4 and _prose_columns(raw_rows):
        return "columns"

    if rows >= MIN_TABLE_ROWS and columns >= 3 and words <= 6:
        return "table"

    return None


def _prose_columns(raw_rows: list) -> bool:
    for index in range(len(raw_rows[0])):
        cells = [row[index] for row in raw_rows if row[index]]
        if len(cells) < 0.6 * len(raw_rows):
            return False
        if mean(_word_count(cell) for cell in cells) < 4:
            return False
    return True


def _trim_trailing(lines: list, rows: list) -> tuple:
    """Drop trailing lines that fill a single cell.

    Left-aligned text just under a table ("Note: fees in INR.") sits inside
    the first column and so does not cross a corridor; it is not a row.
    """
    while len(lines) > 1 and sum(1 for cell in rows[-1] if cell) <= 1:
        lines = lines[:-1]
        rows = rows[:-1]
    return lines, rows


# -- text -------------------------------------------------------------------


def _lines_to_text(lines: list, height: float) -> str:
    out = []
    previous = None
    for line in lines:
        if previous is not None and line.top - previous.bottom > PARAGRAPH_GAP * height:
            out.append("")
        out.append(line.text)
        previous = line
    return "\n".join(out)


def _text_block(lines: list, height: float) -> Block:
    return Block(
        kind="text",
        top=lines[0].top,
        bottom=lines[-1].bottom,
        text=_lines_to_text(lines, height),
    )


def _column_blocks(lines: list, rows: list, bounds: list, height: float) -> list:
    """Page columns, each read top to bottom, left column first."""
    blocks = []
    for index in range(len(bounds) + 1):
        column_lines = []
        for line, cells in zip(lines, rows, strict=True):
            if not cells[index]:
                continue
            words = [
                word
                for word in line.words
                if bisect_right(bounds, word.center_x) == index
            ]
            column_lines.append(Line(words, line.top, line.bottom))
        if column_lines:
            blocks.append(_text_block(column_lines, height))
    return blocks


# -- the page ---------------------------------------------------------------


def layout_page(words, extra_blocks=()) -> list:
    """The page as blocks in reading order.

    `extra_blocks` are pieces the caller already read some other way - a
    ruled table pdfplumber found, text OCR'd out of a picture - whose words
    are NOT in `words`. Each is placed by its `top` among the regions of the
    page, so a ruled table lands between the paragraphs that surround it.
    """
    words = [word for word in words if word.text.strip()]
    extras = sorted(extra_blocks, key=lambda block: block.top)

    if not words:
        return list(extras)

    lines = group_lines(words)
    height = median(word.height for word in words) or 1.0
    gap_min = GAP_FACTOR * height

    regions: list = []  # [blocks], in reading order
    pending: list = []
    waiting = list(extras)

    def flush():
        if pending:
            regions.append([_text_block(pending, height)])
            pending.clear()

    def place_extras_above(limit):
        # An extra block belongs before the first line that starts below its
        # top - even mid-way through what would otherwise be one paragraph
        # run - so it is flushed in between rather than appended after.
        while waiting and waiting[0].top <= limit:
            flush()
            regions.append([waiting.pop(0)])

    index = 0
    while index < len(lines):
        place_extras_above(lines[index].top)
        grown = _grow_band(lines, index, gap_min, height)
        accepted = None

        if grown is not None:
            end, corridors = grown
            bounds = _bounds(corridors)
            band = lines[index:end]
            rows = [_cells(line, bounds) for line in band]
            band, rows = _trim_trailing(band, rows)
            merged = _merge_continuations(band, rows, height)

            if len(merged) >= 2:
                verdict = _classify(rows, merged)
                if verdict == "table" and (
                    len(merged) >= MIN_TABLE_ROWS or _two_row_table_ok(merged)
                ):
                    accepted = (
                        len(band),
                        [
                            Block(
                                kind="table",
                                top=band[0].top,
                                bottom=band[-1].bottom,
                                rows=merged,
                            )
                        ],
                    )
                elif verdict == "columns":
                    accepted = (
                        len(band),
                        _column_blocks(band, rows, bounds, height),
                    )

        if accepted is None:
            pending.append(lines[index])
            index += 1
            continue

        used, blocks = accepted
        flush()
        regions.append(blocks)
        index += used

    flush()
    place_extras_above(float("inf"))

    return [block for blocks in regions for block in blocks]


def _two_row_table_ok(rows: list) -> bool:
    """A header and one data row is a table only with a number in it."""
    return any(_is_number(cell) for cell in rows[1] if cell)
