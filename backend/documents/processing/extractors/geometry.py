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
from math import ceil
from statistics import mean, median

from documents.processing.extractors import wording

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

# A gutter is a column gap that no more than this share of a band's lines run
# through (see `_tolerance`), and the band is long enough for that to be
# at least one line from this many rows on.
GUTTER_TOLERANCE = 0.08
TOLERANCE_FROM = 8

# A line crossing more than this share of the established gutters is not a
# row of the table: it is text that runs across it.
SPAN_FRACTION = 0.25

# Gutters are re-measured every line while a band is this short, then every
# this many lines.
REFRESH_EVERY = 8

# Bounds the quadratic worst case of retrying band starts on a pathological
# page; a real table or column band is far shorter.
MAX_BAND_LINES = 400

MIN_TABLE_ROWS = 3

# Dot leaders: ". . . . ." or "......" between a contents entry and its page.
_LEADERS = re.compile(r"(?:\.\s){3,}\.|\.{6,}")
# What may stand after a contents line's leader: a page number, arabic (up to
# three digits) or lowercase roman (front matter). A decimal ("8.25"), an
# amount ("20000"), a word ("Civil"), a date, or an upper-case numeral (a year
# of study, "III") is a value, not a page.
_PAGE_NUMBER = re.compile(r"^(?:\d{1,3}|(?=[ivx])x{0,2}(?:ix|iv|v?i{0,3}))$")

# A band is a table of contents when this share of its lines (and at least
# two of them) are entries. One entry in a table is not a contents page: an
# invoice has a "Total ........ 500" row and a two-column page has a blank to
# fill in.
CONTENTS_SHARE = 0.5


def _is_contents_line(text: str) -> bool:
    """A leader, and directly after it nothing but a page number."""
    leaders = list(_LEADERS.finditer(text))
    # One leader: a line with several ("Name ..... Roll ..... 12") is a form
    # to fill in, whatever stands at its end.
    if len(leaders) != 1:
        return False
    return bool(_PAGE_NUMBER.match(text[leaders[-1].end() :].strip()))


def _is_contents_band(raw_rows: list) -> bool:
    """Whether most lines of a band are contents entries: leader after leader."""
    lines = [" ".join(cell for cell in row if cell) for row in raw_rows]
    lines = [line for line in lines if line.strip()]
    entries = sum(1 for line in lines if _is_contents_line(line))
    return entries >= max(2, ceil(CONTENTS_SHARE * len(lines)))


def _leading_entries(raw_rows: list) -> int:
    """How many lines at the top of a contents band are entries.

    A band that is mostly entries is text, line by line. Judging it again from
    each of its lines, as a band that fails is, costs more with every line
    of the page; its leading entries are taken at once instead.
    """
    if not _is_contents_band(raw_rows):
        return 0
    count = 0
    for row in raw_rows:
        if not _is_contents_line(" ".join(cell for cell in row if cell)):
            break
        count += 1
    return count


# The largest number a page's line numbering is taken to reach.
MAX_LINE_NUMBER = 60
MIN_COLUMN_LINES = 4

# What a list pulls into its own narrow column: "1.", "(a)", "iv)", a clause
# number like "2.3.4", or a bullet glyph.
_MARKER = re.compile(
    r"^(\(?[0-9A-Za-z]{1,3}[.)]"
    r"|\d{1,3}(\.\d{1,3})+\.?"
    r"|[•◦▪■●○·*\-–—])$"
)
# A cell with nothing in it but punctuation: a colon or a dash used as a
# divider, as transcripts and forms do.
_PUNCTUATION = re.compile(r"^[\W_]+$")
_CURRENCY = re.compile(r"^(rs\.?|inr|usd|eur)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^\d[\d,.:/%\-–]*%?$")


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    # How sure the reader was of this word, 0-100. Layout ignores it; whoever
    # compares two readings of a page does not.
    confidence: float = 100.0

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
    # For a table: True when its first row is known to be the header, or
    # "auto" to let the writer judge (compose.PageBuilder.add_table).
    header: object = "auto"


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


def _covered(line: Line, gap_min: float) -> list:
    """The stretches of x a line has text across, as [x0, x1] pairs.

    Words closer together than a column gap are one stretch: the spaces
    inside a cell are not gutters.
    """
    spans: list = []
    for word in line.words:
        if spans and word.x0 - spans[-1][1] < gap_min:
            spans[-1][1] = max(spans[-1][1], word.x1)
        else:
            spans.append([word.x0, word.x1])
    return spans


def _tolerance(count: int) -> int:
    """How many lines may run through a gutter and still leave it a gutter.

    None for a short band: three rows that all agree are evidence, and one that
    disagrees is a different layout. For a long one a few intruders are noise -
    a long name that nearly touches the next column, a heading that spans two -
    and demanding that a gutter be empty in EVERY row of a fifty-row table
    loses the column the first time one row crowds it.
    """
    allowed = int(GUTTER_TOLERANCE * count)
    if count >= TOLERANCE_FROM and allowed < 1:
        return 1
    return allowed


def _gutters(lines: list, gap_min: float) -> list:
    """Vertical gutters shared by these lines, as (left, right) pairs.

    A gutter is a stretch at least `gap_min` wide that no more than
    `_tolerance` of the lines have text across, with text on both sides of it
    - the empty margin around a table is not a column gap.

    A column between two gutters needs text in at least two of the lines
    (when there are three or more). Without that, one word sitting in the
    middle of a wide gap - a centred heading, a cell wider than its column -
    would split the gap in two and invent a column the page does not have;
    the narrower of the two gutters is dropped and the gap stays whole.
    """
    allowed = _tolerance(len(lines))
    support_needed = 2 if len(lines) >= 3 else 1
    events: list = []
    for line in lines:
        for left, right in _covered(line, gap_min):
            events.append((left, 1))
            events.append((right, -1))
    events.sort()

    found: list = []  # (left, right, lines with text in the column before it)
    coverage = 0
    solid_before = False
    region_start = None
    island = 0
    index = 0

    while index < len(events):
        x = events[index][0]
        while index < len(events) and events[index][0] == x:
            coverage += events[index][1]
            index += 1
        if index >= len(events):
            break

        next_x = events[index][0]
        if coverage > allowed:
            if (
                region_start is not None
                and solid_before
                and x - region_start >= gap_min
            ):
                found.append((region_start, x, island))
                island = 0
            island = max(island, coverage)
            solid_before = True
            region_start = None
        elif solid_before and region_start is None and next_x > x:
            region_start = x

    gutters = [[left, right] for left, right, _ in found]
    supports = [support for _, _, support in found]

    position = 1
    while position < len(gutters):
        if supports[position] < support_needed:
            narrower = min(
                (position - 1, position), key=lambda i: gutters[i][1] - gutters[i][0]
            )
            del gutters[narrower]
            del supports[narrower]
            position = max(position - 1, 1)
            continue
        position += 1

    return [(left, right) for left, right in gutters]


def _crossed(line: Line, gutters: list, gap_min: float) -> int:
    """How many of the gutters this line has text running right across.

    Across means from one side of the gutter to the other, as a sentence runs
    through a column gap. Overlapping is not enough: the gutters of a band's
    first line are only the gaps between ITS words, and figures right-aligned
    under left-aligned headings always sit partly - even mostly - inside them
    without being text that runs through a gap.
    """
    spans = _covered(line, gap_min)
    return sum(
        1
        for left, right in gutters
        if any(
            span_left <= left and span_right >= right for span_left, span_right in spans
        )
    )


# -- bands ------------------------------------------------------------------


def _grow_band(lines: list, start: int, gap_min: float, height: float):
    """The longest run of lines from `start` that share their gutters.

    Returns (end, gutters) or None. The first line must itself be laid out in
    at least two segments. A later line ends the band if it has text across a
    meaningful share of the gutters found so far - a full-width sentence does,
    a table row does not. One that stays inside a column (a wrapped cell) goes
    on being part of it.
    """
    if len(_segments(lines[start], gap_min)) < 2:
        return None

    gutters = _gutters(lines[start : start + 1], gap_min)
    end = start + 1

    while end < len(lines) and end - start < MAX_BAND_LINES:
        previous, line = lines[end - 1], lines[end]
        if line.top - previous.bottom > MAX_ROW_GAP * height:
            break
        if gutters and _crossed(line, gutters, gap_min) > SPAN_FRACTION * len(gutters):
            break
        end += 1

        # Re-measured as the band grows - every line while it is short, then
        # every few - so the gutters reflect the rows, not just the first.
        count = end - start
        if count <= REFRESH_EVERY or count % REFRESH_EVERY == 0:
            gutters = _gutters(lines[start:end], gap_min) or gutters

    return end, _gutters(lines[start:end], gap_min)


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


# Public: the ruled-table reader asks the same question of a cell's lines.
is_number = _is_number


def _word_count(cell: str) -> int:
    return len(cell.split())


def _is_line_numbers(cells: list) -> bool:
    """Consecutive small whole numbers: a gutter of line numbers, not data.

    Small, because a run of years - 2019, 2020, 2021 - or of course codes - 101,
    102, 103 - is a column of a table. A page rarely has more than sixty lines.
    """
    try:
        values = [int(cell) for cell in cells]
    except ValueError:
        return False
    return (
        len(values) >= 3
        and max(values) <= MAX_LINE_NUMBER
        and all(
            later - earlier == 1
            for earlier, later in zip(values, values[1:], strict=False)
        )
    )


def _columns_with_content(rows: list) -> list:
    """(index, cells) for each column that has text in enough rows to count.

    Enough is two rows, or a third of them: one stray fragment in a column -
    "No. 07-1315", "2009" split off a date by a wide gap - is not a column.
    A column of nothing but punctuation is a divider, as in a transcript's
    caption box, and is not one either.
    """
    needed = max(2, -(-3 * len(rows) // 10))
    columns = []
    for index in range(len(rows[0])):
        cells = [row[index] for row in rows if row[index]]
        if len(cells) < needed:
            continue
        if all(_PUNCTUATION.match(cell) for cell in cells):
            continue
        columns.append((index, cells))
    return columns


def _gutter_columns(rows: list) -> set:
    """Indexes of columns that are line numbers, not data.

    A column of consecutive small whole numbers beside text is the numbering of
    a transcript or a statute - unless another column is numeric, in which case
    it is just one of the table's figures (semester 1, 2, 3 beside credits).
    """
    columns = _columns_with_content(rows)
    data_numbers = any(
        not _is_line_numbers(cells) and sum(map(_is_number, cells)) >= 0.6 * len(cells)
        for _, cells in columns
    )
    if data_numbers:
        return set()
    return {index for index, cells in columns if _is_line_numbers(cells)}


def _substantive_columns(rows: list) -> int:
    """How many columns carry content of their own.

    Two columns of content make a table. A gutter of line numbers, one column of
    text and a colon make a transcript.
    """
    gutters = _gutter_columns(rows)
    return sum(1 for index, _ in _columns_with_content(rows) if index not in gutters)


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

    # Dot leaders run from an entry to its page number: a table of contents,
    # whose entries are lines. Read as a table, a sub-entry with nothing in
    # the number column folds into the entry above it. Only a band that is
    # mostly such entries: one row with a leader is a table with a leader.
    if _is_contents_band(raw_rows):
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

    if _substantive_columns(merged_rows) < 2:
        return None

    gutters = _gutter_columns(merged_rows)
    numeric_columns = 0
    for index in range(columns):
        if index in gutters:
            continue
        cells = [row[index] for row in body if row[index]]
        if len(cells) >= 2 and sum(map(_is_number, cells)) >= 0.6 * len(cells):
            numeric_columns += 1

    words = mean(_word_count(cell) for cell in filled)
    rows = len(merged_rows)

    if numeric_columns:
        # Numbers beside a paragraph of prose is a numbered clause, whatever
        # it looks like; a table's cells are short.
        return "table" if words <= 15 else None

    # A label beside its value - "Name | Asha Rao" - is a form, and a form
    # is a table. One short column is what sets it apart from prose columns,
    # whose cells are all a line of a sentence long. (A label beside several
    # lines of text - "UNIT I" beside its topics, "[10]" beside a question -
    # is the same thing: the label belongs to the lines beside it, and must
    # stay with them.)
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
    """Lines as text, a blank line between paragraphs.

    A word hyphenated across two lines of one paragraph is joined
    (`wording.dehyphenate`); across a paragraph break nothing is. A symbol-font
    bullet that starts a line is written as a bullet (`wording.bullet_line`);
    the same glyph anywhere else is a letter, an arrow or a tick, and stays.
    """
    paragraphs: list = [[]]
    previous = None
    for line in lines:
        if previous is not None and line.top - previous.bottom > PARAGRAPH_GAP * height:
            paragraphs.append([])
        paragraphs[-1].append(wording.bullet_line(line.text))
        previous = line
    return "\n\n".join(
        "\n".join(wording.dehyphenate(paragraph)) for paragraph in paragraphs
    )


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

            entries = _leading_entries(rows)
            if entries and waiting:
                # A block placed by its position still lands between the
                # lines it lies among.
                for step in range(1, entries):
                    if waiting[0].top <= lines[index + step].top:
                        entries = step
                        break
            if entries:
                pending.extend(lines[index : index + entries])
                index += entries
                continue

            if len(merged) >= MIN_TABLE_ROWS:
                verdict = _classify(rows, merged)
                if verdict == "table":
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
