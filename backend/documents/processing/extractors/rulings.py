"""Ruled tables in a PDF: which lines are really drawn, and what the grid means.

pdfplumber finds a ruled table from the edges of every line and rectangle on
the page, and it reads the result as one rectangular grid. Both steps go wrong
on ordinary office documents, in ways that cost more than a missed table:

*Invisible rectangles become rulings.* Word, when it saves a PDF, paints a
white rectangle behind every table cell and another behind every paragraph
inside it, inset a few points from the cell's borders - and in a shaded table
it paints them in the cell's own colour. None of them can be seen: white on
white paper, green on the green it sits on. As rulings they cut each cell into
a frame of thin phantom columns and one phantom row per line of text, so a
three-column table came back with seven columns, a wrapped description became
three rows, and "database" was split as "d" | "atabase". `visible_page` takes
away exactly those: a filled rectangle with no outline whose colour is the
colour behind it. Anything drawn with a stroke - a line, a border - stays a
ruling, whatever its colour: a stroke's colour cannot always be read (a spot
colour arrives as a bare tint) and what it is drawn over cannot always be
told (paint order, transparency), and a real rule taken away costs a table.

*One grid is not one table.* Two tables drawn touching - a list of courses
and, under the same border, a list of elective streams - are found as one,
and every column boundary of either cuts through the cells of both. `read`
splits the grid where the rows' columns change for good: a row whose columns
cross the columns above it AND add boundaries of their own, by more than the
few points a hand-edited Word table drifts, and the row after it doing the
same. A header that subdivides ("Periods/Week" over L, T, P) or a total row
that spans does only one of those, and stays; one jagged row stays; a first
row is never cut off from the rows under it; a title row just above the
change ("ELECTIVE STREAMS") goes with the table it names.

*Merged cells.* A grid position no cell starts at is covered by a merged one,
which pdfplumber reports as None and a blank cell as "". The difference is
kept: a label merged down a group of rows ("Pool-I" beside its four courses)
belongs to every record of the group, so it is written into each, while a
blank cell stays blank and a merged figure (a group total, a shared credit
value) stays once, where it is printed - written into every row, a column's
sum would count it once per row. A first row that is only a title stays the
table's first row - and so its header, as every reader of a table takes the
header to be its first row. A header split
over two rows becomes one row whose columns read "Periods/Week L",
"Periods/Week T" - the form every later reader of a row (search, the
answerer, table arithmetic) needs - but only when its cells are laid out as
a header's are; a form's first rows beside a merged photo cell are rows.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass, field

from documents.processing.extractors import compose

# Two colours closer than this on every RGB channel (0-1) are the same colour
# to a reader: an edge between them is not visible.
SAME_COLOUR = 0.02

# Coordinates closer than this (points) are the same boundary.
SNAP = 1.0

# A filled rectangle this thin (points), and at least LINE_LENGTH long, is a
# line drawn as a rectangle - the border of a cell, as browsers print them -
# and a border in the colour of what it lies on is the only thing that says
# where the cells of a dark header row end.
LINE_THICKNESS = 3.0
LINE_LENGTH = 8.0

# What lies behind a filled rectangle is looked up among the fills that
# overlap the same square of the page; the page is cut into this many
# squares each way.
BACKGROUND_BUCKETS = 16

# A merged cell's text is written into every row it spans only when it is a
# label; a merged paragraph stays in its first row.
FILL_DOWN_MAX_CHARS = 60
FILL_DOWN_MAX_WORDS = 8

WHITE = (1.0, 1.0, 1.0)


# -- what is visible ---------------------------------------------------------


def _rgb(colour):
    """A pdfplumber colour as (r, g, b) in 0-1, or None when it cannot be told.

    PDF's initial fill and stroke colour is black, so an unset colour is
    black. A pattern or a named colour space gives no numbers to compare and
    is treated as visible.
    """
    if colour is None:
        return (0.0, 0.0, 0.0)
    if isinstance(colour, (int, float)):
        value = float(colour)
        return (value, value, value)
    if isinstance(colour, (list, tuple)):
        if not all(isinstance(value, (int, float)) for value in colour):
            return None
        values = [float(value) for value in colour]
        if len(values) == 1:
            return (values[0],) * 3
        if len(values) == 3:
            return tuple(values)
        if len(values) == 4:
            c, m, y, k = values
            return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def _same(a, b) -> bool:
    if a is None or b is None:
        return False
    return all(abs(x - y) <= SAME_COLOUR for x, y in zip(a, b, strict=True))


def _is_line(rect) -> bool:
    """A rectangle that is a line: thin one way, long the other."""
    width = max(rect["x1"] - rect["x0"], 0.0)
    height = max(rect["bottom"] - rect["top"], 0.0)
    return min(width, height) <= LINE_THICKNESS and max(width, height) >= LINE_LENGTH


def _area(obj) -> float:
    return max(obj["x1"] - obj["x0"], 0.0) * max(obj["bottom"] - obj["top"], 0.0)


def _contains(outer, inner) -> bool:
    return (
        outer["x0"] - SNAP <= inner["x0"]
        and inner["x1"] <= outer["x1"] + SNAP
        and outer["top"] - SNAP <= inner["top"]
        and inner["bottom"] <= outer["bottom"] + SNAP
    )


class _Backgrounds:
    """The fills of one page, ready to say which one lies behind a shape.

    Behind a shape is the smallest larger fill around it, or the paper. Any
    fill around a shape covers the shape's centre, so only the fills that
    overlap the centre's square of the page are candidates, smallest first:
    the first that contains the shape is the answer. Each shape costs the
    handful of fills near it, not every fill on the page.
    """

    def __init__(self, fills, bbox):
        x0, top, x1, bottom = bbox
        self._x0, self._top = x0, top
        self._width = max(x1 - x0, 1.0) / BACKGROUND_BUCKETS
        self._height = max(bottom - top, 1.0) / BACKGROUND_BUCKETS
        self._buckets: dict = {}
        for rect, colour in sorted(fills, key=lambda item: _area(item[0])):
            entry = (_area(rect), rect, colour)
            for column in range(
                self._column(rect["x0"] - SNAP), self._column(rect["x1"] + SNAP) + 1
            ):
                for row in range(
                    self._row(rect["top"] - SNAP), self._row(rect["bottom"] + SNAP) + 1
                ):
                    self._buckets.setdefault((column, row), []).append(entry)
        self._areas = {
            key: [area for area, _, _ in entries]
            for key, entries in self._buckets.items()
        }

    def _column(self, x) -> int:
        return min(max(int((x - self._x0) // self._width), 0), BACKGROUND_BUCKETS - 1)

    def _row(self, y) -> int:
        return min(max(int((y - self._top) // self._height), 0), BACKGROUND_BUCKETS - 1)

    def behind(self, obj) -> tuple:
        key = (
            self._column((obj["x0"] + obj["x1"]) / 2),
            self._row((obj["top"] + obj["bottom"]) / 2),
        )
        entries = self._buckets.get(key, ())
        if not entries:
            return WHITE
        area = _area(obj)
        start = bisect_right(self._areas[key], area * 1.001)
        for _, fill, colour in entries[start:]:
            if fill is not obj and _contains(fill, obj):
                return colour
        return WHITE


def invisible(page) -> set:
    """ids of the filled rectangles on this page that draw no visible edge.

    Only a rectangle filled and not stroked can be invisible, and only when
    its colour is the colour behind it. Lines, stroked rectangles and
    rectangles thin enough to be lines are always kept: they are what a table
    is ruled with, in whatever colour.
    """
    fills = [
        (rect, _rgb(rect.get("non_stroking_color")))
        for rect in page.rects
        if rect.get("fill")
    ]
    plain = [
        (rect, colour)
        for rect, colour in fills
        if not rect.get("stroke") and not _is_line(rect)
    ]
    if not plain:
        return set()
    backgrounds = _Backgrounds(fills, page.bbox)
    return {
        id(rect) for rect, colour in plain if _same(colour, backgrounds.behind(rect))
    }


def visible_page(page):
    """The page with every filled rectangle nobody could see taken away.

    Characters, pictures, curves, lines and stroked rectangles are untouched;
    only the shapes the table finder would otherwise take for rulings are
    filtered.
    """
    hidden = invisible(page)
    if not hidden:
        return page
    return page.filter(lambda obj: id(obj) not in hidden)


# -- reading a grid ----------------------------------------------------------

# A cell this many lines tall with a column gap running through it is not a
# cell: it is a region the rulings did not divide - a statement body under a
# ruled header, a table inside one box. Its words are left to the layout
# pass, which finds the rows and columns from the text itself.
CONTAINER_LINES = 3
# A column gap inside a cell is at least this many median character heights.
# Width alone cannot tell it from the gap between a bullet and its text -
# word processors indent list text 18 points past the bullet whatever the
# font size, which at 9 points is wider than 1.5 character heights, while
# the columns of a precinct bulletin's boxes are 15 points apart - so a gap
# with only list markers on its left is never a column gap (`_has_gutter`).
CONTAINER_GUTTER = 1.5
# A row is left to the layout pass only when every cell of it with text in it
# holds a table of its own: a box across the whole table, the four boxes of a
# precinct bulletin. A row with an ordinary cell beside one - an invoice line
# whose description has its details in two columns, a skills row whose list
# runs in two - is an ordinary row, read as cells.

# Hand-edited Word tables are edited row by row, and a row's borders can sit
# this many points off the same borders in the rows above it. Within this a
# boundary is the same boundary when deciding where one table ends.
EDGE_JITTER = 8.0
# Column boundaries closer than this are one column of the grid.
COLUMN_TOLERANCE = 6.0

# A first row whose only text is one cell this wide (share of the table) is
# the table's title.
TITLE_SHARE = 0.6

# A list marker at the start of a line: a bullet or dash, "1." "1.1" "(a)".
_MARKER = re.compile(
    r"^(?:\(?[0-9A-Za-z]{1,3}[.)]|\d{1,3}(?:\.\d{1,3})+\.?|[•◦▪"
    r"■●○·‣⁃➢✓✔*\-–—>o])$"
)
# A figure with its unit, or a sum of them: "9 Hrs", "10M", "45 min", "5 x 2 =
# 10M". Written into every row of a merged cell it would be a value per row,
# which it is not - it is the whole group's. An ordinal ("1st Year") is a label.
_MEASURE = re.compile(
    r"^\(?[+\-\u2212]?(?:rs\.?|inr|usd|eur|gbp|[$\u20ac\u00a3\u20b9\u00a5])?\s*"
    r"\d[\d,.]*\s*(?:%|(?!(?:st|nd|rd|th)\b)[A-Za-z]{1,10}\.?)?\)?"
    r"(?:\s*[x\u00d7*=+/\-]\s*\(?\d[\d,.]*\s*(?:%|[A-Za-z]{1,10}\.?)?\)?)*$",
    re.IGNORECASE,
)
# Three letters in a row: a word, not a figure, a dash or a code like "C2".
_LETTERS = re.compile(r"[^\W\d_]{3}")
_WORDY = re.compile(r"[^\W_]")


@dataclass(eq=False)
class _Cell:
    bbox: tuple
    row: int  # index of the row the cell starts in
    chars: list = field(default_factory=list)
    text: str = ""
    lines: int = 0
    col: int = 0  # set per segment
    colspan: int = 1
    rowspan: int = 1


@dataclass
class Grid:
    """One table read out of a ruled grid."""

    bbox: tuple
    rows: list
    # True when the first row after the title was built from a header split
    # over several rows, so it is known to be a header rather than guessed.
    header: bool = False
    filled_cells: int = 0


def _snapped(values) -> list:
    out: list = []
    for value in sorted(values):
        if not out or value - out[-1] > SNAP:
            out.append(value)
    return out


def _count(boundaries, start, end) -> int:
    """How many of `boundaries` fall in [start, end)."""
    return sum(1 for b in boundaries if start - SNAP <= b < end - SNAP)


def _cells(table) -> tuple:
    """([_Cell], row tops) from a pdfplumber Table, without text yet."""
    tops = []
    cells = []
    for index, row in enumerate(table.rows):
        tops.append(row.bbox[1])
        for bbox in row.cells:
            if bbox is not None:
                cells.append(_Cell(bbox=tuple(bbox), row=index))
    return cells, tops


def _assign_chars(table, cells, tops) -> None:
    """Give every character inside the table to the cell its centre is in.

    The same rule pdfplumber's own `Table.extract` applies, done once for the
    whole table by position instead of once per cell, so a cell's text can be
    read in the direction its own characters run.
    """
    columns = _snapped(cell.bbox[0] for cell in cells)
    where: dict = {}
    for cell in cells:
        first_col = max(0, bisect_right(columns, cell.bbox[0] + SNAP) - 1)
        span_cols = max(1, _count(columns, cell.bbox[0], cell.bbox[2]))
        span_rows = max(1, _count(tops, cell.bbox[1], cell.bbox[3]))
        for r in range(cell.row, cell.row + span_rows):
            for c in range(first_col, first_col + span_cols):
                where.setdefault((r, c), cell)

    x0, top, x1, bottom = table.bbox
    for char in table.page.chars:
        cx = (char["x0"] + char["x1"]) / 2
        cy = (char["top"] + char["bottom"]) / 2
        if not (x0 <= cx < x1 and top <= cy < bottom):
            continue
        cell = where.get((bisect_right(tops, cy) - 1, bisect_right(columns, cx) - 1))
        if cell is None:
            continue
        b = cell.bbox
        if b[0] <= cx < b[2] and b[1] <= cy < b[3]:
            cell.chars.append(char)


def _is_text(words: list) -> bool:
    """Whether words on one side of a gap say something: more than markers."""
    content = [word for word in words if not _MARKER.match(word)]
    return sum(len(_WORDY.findall(word)) for word in content) >= 2


def _has_gutter(chars) -> bool:
    """Whether a column gap runs through these characters from top to bottom.

    Spaces between words do not count: across several lines of prose the
    words of one line cover the spaces of the next. A gap no character of any
    line falls in, CONTAINER_GUTTER character heights wide or more, is a
    column gap when at least two lines have text on each side of it - more
    than a list marker: the gap between a column of bullets or numbers and
    the items they mark is a list's indent.
    """
    printed = [char for char in chars if char["text"].strip()]
    if len(printed) < 2:
        return False
    heights = sorted(char["bottom"] - char["top"] for char in printed)
    height = heights[len(heights) // 2] or 1.0

    spans: list = []
    for char in sorted(printed, key=lambda c: c["x0"]):
        if spans and char["x0"] <= spans[-1][1] + 0.6 * height:
            spans[-1][1] = max(spans[-1][1], char["x1"])
        else:
            spans.append([char["x0"], char["x1"]])

    lines: dict = {}
    for char in printed:
        lines.setdefault(round(char["top"]), []).append(char)

    for left, right in zip(spans, spans[1:], strict=False):
        if right[0] - left[1] < CONTAINER_GUTTER * height:
            continue
        before = after = 0
        for line in lines.values():
            words = _words_of(line)
            if _is_text([text for text, x in words if x < right[0]]):
                before += 1
            if _is_text([text for text, x in words if x >= right[0]]):
                after += 1
        if before >= 2 and after >= 2:
            return True
    return False


def _words_of(chars) -> list:
    """[(word, x0)] of one line's characters, split where a space would be."""
    words: list = []
    for char in sorted(chars, key=lambda c: c["x0"]):
        size = char["bottom"] - char["top"] or 1.0
        if words and char["x0"] - words[-1][2] <= 0.25 * size:
            words[-1][0] += char["text"]
            words[-1][2] = char["x1"]
        else:
            words.append([char["text"], char["x0"], char["x1"]])
    return [(text, x0) for text, x0, _ in words]


def _rows_of(cell, tops) -> range:
    return range(cell.row, cell.row + max(1, _count(tops, cell.bbox[1], cell.bbox[3])))


def _container_rows(cells, tops) -> set:
    """Rows taken up by cells that hold a table of their own.

    Merged across the columns or not: a precinct bulletin ruled as four tall
    boxes, each a column of "NAME  PARTY  VOTES" lines, has no merged cell,
    and reading each box as one cell flattens forty rows into one. A row is
    left to the layout pass only when every cell of it with text in it is
    such a cell; beside an ordinary cell, two columns of text in one cell
    are that cell's content. A merged cell reaching into such a row goes
    with it, whole: cut at the row, its text would be read twice - in the
    table and again by the layout pass.
    """
    containers = {
        id(cell)
        for cell in cells
        if cell.lines >= CONTAINER_LINES and _has_gutter(cell.chars)
    }
    if not containers:
        return set()

    covering: dict = {}
    for cell in cells:
        for r in _rows_of(cell, tops):
            covering.setdefault(r, []).append(cell)

    rows = {
        r
        for r, present in covering.items()
        if any(id(c) in containers for c in present)
        and all(id(c) in containers for c in present if c.text.strip())
    }

    # Only a cell with text in it could be read twice; an empty merged cell
    # can be cut anywhere.
    written = [cell for cell in cells if cell.text.strip()]
    while True:
        reaching = {
            r
            for cell in written
            for r in _rows_of(cell, tops)
            if rows.intersection(_rows_of(cell, tops))
        }
        if reaching <= rows:
            return rows
        rows |= reaching


def _edges(row_cells) -> set:
    return {c.bbox[0] for c in row_cells} | {c.bbox[2] for c in row_cells}


def _breaks(row_cells, edges) -> bool:
    """Whether a row's columns are not the columns of `edges`.

    It adds a boundary of its own - further from theirs than a hand-edited
    row drifts - and one of its cells runs across a boundary of theirs. A
    row whose borders all sit within EDGE_JITTER of the rows above has no
    boundary of its own, wherever its cells end.
    """
    new = [
        e
        for e in _edges(row_cells)
        if not any(abs(e - old) <= EDGE_JITTER for old in edges)
    ]
    if not new:
        return False
    return any(
        c.bbox[0] + SNAP < e < c.bbox[2] - SNAP for c in row_cells for e in edges
    )


def _is_title_row(row_cells, span) -> bool:
    """Whether the cells starting in a row are a title: one cell of text,
    TITLE_SHARE of the table wide or more, nothing else written."""
    texts = [cell for cell in row_cells if cell.text.strip()]
    return len(texts) == 1 and texts[0].bbox[2] - texts[0].bbox[0] >= TITLE_SHARE * span


def _segments(cells, tops, excluded) -> list:
    """Row ranges [start, end) of the separate tables inside one grid.

    Rows in `excluded` belong to no table and end the one above them. A
    title row just above the row where the columns change ("ELECTIVE
    STREAMS" over the streams) is the first row of the table it names.
    """
    by_row: dict = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)
    span = max(c.bbox[2] for c in cells) - min(c.bbox[0] for c in cells)

    segments = []
    start = None
    edges: list = []
    reach = -1  # the last row any cell of this segment extends into
    reached = -1  # the same, before the row above this one
    for index in range(len(tops)):
        if index in excluded:
            if start is not None:
                segments.append((start, index))
                start = None
            continue
        if start is None:
            start, edges, reach, reached = index, [], index, index - 1

        row_cells = by_row.get(index, [])
        following = index + 1
        if (
            # A first row is never cut off from the rows under it, and a
            # merged cell from above never cut through.
            index > start + 1
            and reach < index
            and _breaks(row_cells, edges)
            # The new columns hold for the next row too: one jagged row is
            # not a second table.
            and following < len(tops)
            and following not in excluded
            and _breaks(by_row.get(following, []), edges)
        ):
            cut = index
            above = by_row.get(index - 1, [])
            # Not when a merged cell from higher up reaches into the title
            # row: the row then belongs to that cell's group.
            if reached < index - 1 and _is_title_row(above, span):
                cut = index - 1
            segments.append((start, cut))
            start, edges = cut, list(_edges(above)) if cut < index else []
        edges.extend(_edges(row_cells))
        reached = reach
        for cell in row_cells:
            reach = max(reach, index + _count(tops, cell.bbox[1], cell.bbox[3]) - 1)

    if start is not None:
        segments.append((start, len(tops)))
    return segments


def _column_positions(values) -> dict:
    """Each x coordinate mapped to the column boundary it belongs to.

    Boundaries within COLUMN_TOLERANCE of each other are one: a border one
    row dragged a few points is the same column, not a new one.
    """
    groups: list = []
    for value in sorted(set(values)):
        if groups and value - groups[-1][-1] <= COLUMN_TOLERANCE:
            groups[-1].append(value)
        else:
            groups.append([value])
    where = {}
    for group in groups:
        middle = sorted(group)[len(group) // 2]
        for value in group:
            where[value] = middle
    return where


def _is_short(text: str) -> bool:
    flat = " ".join(text.split())
    return len(flat) <= FILL_DOWN_MAX_CHARS and len(flat.split()) <= FILL_DOWN_MAX_WORDS


def _is_label(text: str) -> bool:
    """Short text with a letter in it that is not a figure or a measure."""
    flat = " ".join(text.split())
    return (
        _is_short(flat)
        and any(ch.isalpha() for ch in flat)
        and not _MEASURE.match(flat)
    )


def _fills(cell, mine, first_body_row) -> bool:
    """Whether a merged `cell` is written again into a row it reaches down to.

    `mine` are the cells starting in that row; `first_body_row` is the index
    (in the whole grid) of the first row under the title and header rows.

    Only a group's label is: short text that is not a figure ("Pool-I"),
    starting in the body - in a title or header row a cell merged down is a
    header's, or a form's photo - and written only into a row that names
    something of its own to the label's right: a record of the group. A
    merged figure (a part's total marks, an elective slot's credits) stays
    once, in its first row: written into every row, a sum over the column
    would count it once per row. A row whose own cells are only figures or
    dashes (the L, T, P values under a course's own row) continues the
    record above it and gets nothing.
    """
    return (
        cell.row >= first_body_row
        and _is_label(cell.text)
        and any(own.col > cell.col and _LETTERS.search(own.text) for own in mine)
    )


def _cells_of(row) -> list:
    """The distinct cells covering one grid row, left to right."""
    out: list = []
    for cell in row:
        if cell is not None and (not out or out[-1] is not cell):
            out.append(cell)
    return out


def _is_title(grid, span) -> bool:
    """Whether the first row of a grid with rows under it is a title."""
    return len(grid) >= 2 and _is_title_row(_cells_of(grid[0]), span)


def _header_depth(grid, first, start) -> int:
    """How many rows from `first` make up one header split over rows.

    1 unless the rows are laid out as a header is: every cell of the first
    row either runs down the whole header ("Course Code") or runs across
    columns the rows below subdivide ("Periods/Week" over L, T, P), and every
    cell starting below lies entirely under such a cell. A form's first rows
    - a photo merged down beside "Name", with "Father's Name" starting in the
    next row under nothing - are not. Neither is a grid with no row left
    under the header.
    """
    top = _cells_of(grid[first])
    if any(cell.row - start != first for cell in top):
        return 1
    depth = max(cell.rowspan for cell in top)
    if not 2 <= depth <= 3 or first + depth >= len(grid):
        return 1
    rows = grid[first : first + depth]
    if any(cell is None for row in rows for cell in row):
        return 1

    below = []
    for r, row in enumerate(rows[1:], start=first + 1):
        for cell in _cells_of(row):
            if cell.row - start == r and cell not in below:
                below.append(cell)

    for cell in below:
        if cell.row - start + cell.rowspan > first + depth:
            return 1
        if not any(
            parent.colspan > 1
            and parent.col <= cell.col
            and cell.col + cell.colspan <= parent.col + parent.colspan
            for parent in top
        ):
            return 1
    # Every grid position is covered and every cell starting below lies under
    # a cell merged across, so each first-row cell either runs down the whole
    # header or is merged across and subdivided below it.
    return depth


def _combined_header(grid, first, depth) -> list:
    combined = []
    for c in range(len(grid[first])):
        parts: list = []
        seen: set = set()
        for r in range(first, first + depth):
            cell = grid[r][c]
            if cell is None or id(cell) in seen:
                continue
            seen.add(id(cell))
            text = " ".join(cell.text.split())
            if text and text not in parts:
                parts.append(text)
        combined.append(" ".join(parts))
    return combined


def _read_segment(cells, tops, start, end, table_bottom) -> Grid | None:
    own = [cell for cell in cells if start <= cell.row < end]
    if not own:
        return None

    row_tops = tops[start:end]
    bottom = tops[end] if end < len(tops) else table_bottom
    at = _column_positions([c.bbox[0] for c in own] + [c.bbox[2] for c in own])
    columns = _snapped(at[cell.bbox[0]] for cell in own)

    width = len(columns)
    height = len(row_tops)
    grid = [[None] * width for _ in range(height)]
    for cell in own:
        x0, x1 = at[cell.bbox[0]], at[cell.bbox[2]]
        cell.col = max(0, bisect_right(columns, x0 + SNAP) - 1)
        cell.colspan = max(1, _count(columns, x0, x1))
        cell.rowspan = max(1, _count(row_tops, cell.bbox[1], min(cell.bbox[3], bottom)))
        local_row = cell.row - start
        for r in range(local_row, min(local_row + cell.rowspan, height)):
            for c in range(cell.col, min(cell.col + cell.colspan, width)):
                grid[r][c] = cell

    left = min(cell.bbox[0] for cell in own)
    right = max(cell.bbox[2] for cell in own)
    result = Grid(
        bbox=(left, row_tops[0], right, max(min(cell.bbox[3], bottom) for cell in own)),
        rows=[],
    )

    def starts_here(cell, r):
        return cell is not None and cell.row - start == r

    first = 0
    if _is_title(grid, right - left):
        first = 1

    body_from = first
    depth = _header_depth(grid, first, start)
    combined = _combined_header(grid, first, depth) if depth > 1 else []
    # The combined row is the header only when it reads as one: some text,
    # and no cell that reads as a value. Otherwise the rows stay as drawn.
    if any(combined) and not any(map(compose.looks_like_value, combined)):
        result.header = True
        body_from = first + depth
    first_body_row = start + max(body_from, first + 1)

    def written(r):
        """Row r as cells; a merged group label also in each record it spans."""
        mine = [cell for cell in _cells_of(grid[r]) if starts_here(cell, r)]
        row = []
        for c in range(width):
            cell = grid[r][c]
            if cell is None or c != cell.col:
                row.append("")
            elif starts_here(cell, r):
                row.append(cell.text)
            elif _fills(cell, mine, first_body_row):
                row.append(cell.text)
                result.filled_cells += 1
            else:
                row.append("")
        return row

    if first:
        result.rows.append(written(0))
    if result.header:
        result.rows.append(combined)
    for r in range(body_from, height):
        if not any(starts_here(cell, r) for cell in grid[r]):
            # Every cell in this row continues one from above: it would only
            # repeat the row before it.
            continue
        result.rows.append(written(r))

    return result


def read(table, text_of) -> list:
    """The tables (`Grid`s) inside one pdfplumber ruled table, top to bottom.

    `text_of(chars) -> str` reads one cell's characters; the caller knows
    which way text runs and how words are spaced.
    """
    cells, tops = _cells(table)
    if not cells:
        return []
    _assign_chars(table, cells, tops)
    for cell in cells:
        cell.text = text_of(cell.chars) if cell.chars else ""
        cell.lines = cell.text.count("\n") + 1 if cell.text.strip() else 0

    excluded = _container_rows(cells, tops)
    grids = []
    for start, end in _segments(cells, tops, excluded):
        grid = _read_segment(cells, tops, start, end, table.bbox[3])
        if grid is not None and grid.rows:
            grids.append(grid)
    return grids
