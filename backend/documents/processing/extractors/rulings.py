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
three rows, and "database" was split as "d" | "atabase". `visible_page` keeps
only what draws an edge someone could see: a line or border whose colour
differs from whatever lies behind it, or a fill whose colour does.

*One grid is not one table.* Two tables drawn touching - a list of courses
and, under the same border, a list of elective streams - are found as one,
and every column boundary of either cuts through the cells of both. `read`
splits the grid wherever a row's columns cross the columns above it AND add
boundaries of their own: a header that subdivides ("Periods/Week" over L, T,
P) or a total row that spans does only one of those, and stays.

*Merged cells.* A grid position no cell starts at is covered by a merged one,
which pdfplumber reports as None and a blank cell as "". The difference is
kept: a value merged down a group of rows ("Pool-I", its 4 credits) belongs to
every row of the group, so it is written into each, while a blank cell stays
blank. A first row that spans the whole table is its title, not a row of
blanks. A header split over two rows becomes one row whose columns read
"Periods/Week L", "Periods/Week T" - the form every later reader of a row
(search, the answerer, table arithmetic) needs.
"""

from bisect import bisect_right
from dataclasses import dataclass, field

# Two colours closer than this on every RGB channel (0-1) are the same colour
# to a reader: an edge between them is not visible.
SAME_COLOUR = 0.02

# A rectangle this thin is a drawn line, not a filled area.
LINE_THICKNESS = 2.0

# Coordinates closer than this (points) are the same boundary.
SNAP = 1.0

# Finding what lies behind each shape compares it with every filled area
# bigger than it; past this many fills only white-on-white is removed.
MAX_FILLS_FOR_BACKGROUND = 400

# A merged cell's text is written into every row it spans only when it is a
# label or a value; a merged paragraph stays in its first row.
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


def _area(obj) -> float:
    return max(obj["x1"] - obj["x0"], 0.0) * max(obj["bottom"] - obj["top"], 0.0)


def _contains(outer, inner) -> bool:
    return (
        outer["x0"] - SNAP <= inner["x0"]
        and inner["x1"] <= outer["x1"] + SNAP
        and outer["top"] - SNAP <= inner["top"]
        and inner["bottom"] <= outer["bottom"] + SNAP
    )


def _background(obj, fills) -> tuple:
    """The colour behind `obj`: the smallest larger fill around it, or paper."""
    area = _area(obj)
    best = None
    for fill, colour in fills:
        if fill is obj or _area(fill) <= area * 1.001:
            continue
        if _contains(fill, obj) and (best is None or _area(fill) < _area(best[0])):
            best = (fill, colour)
    return best[1] if best else WHITE


def invisible(page) -> set:
    """ids of the lines and rectangles on this page that draw no visible edge."""
    rects = page.rects
    fills = [
        (rect, _rgb(rect.get("non_stroking_color")))
        for rect in rects
        if rect.get("fill")
    ]
    nested = len(fills) <= MAX_FILLS_FOR_BACKGROUND

    def behind(obj):
        return _background(obj, fills) if nested else WHITE

    hidden = set()
    for rect in rects:
        stroked = rect.get("stroke") and not _same(
            _rgb(rect.get("stroking_color")), behind(rect)
        )
        filled = rect.get("fill") and not _same(
            _rgb(rect.get("non_stroking_color")), behind(rect)
        )
        if not stroked and not filled:
            hidden.add(id(rect))

    for line in page.lines:
        if _same(_rgb(line.get("stroking_color")), behind(line)):
            hidden.add(id(line))

    return hidden


def visible_page(page):
    """The page with every line and rectangle nobody could see taken away.

    Characters, pictures and curves are untouched; only the shapes the table
    finder would otherwise take for rulings are filtered.
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
# A column gap inside a cell, in median character heights.
CONTAINER_GUTTER = 1.5


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
    caption: str = ""
    # True when the first row was built from a header split over several rows,
    # so it is known to be a header rather than guessed.
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


def _has_gutter(chars) -> bool:
    """Whether a column gap runs through these characters from top to bottom.

    Spaces between words do not count: across several lines of prose the
    words of one line cover the spaces of the next. A gap no character of any
    line falls in, with text on both sides in at least two lines, is a
    column gap.
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
            spans[-1][2].add(round(char["top"]))
        else:
            spans.append([char["x0"], char["x1"], {round(char["top"])}])

    for left, right in zip(spans, spans[1:], strict=False):
        if right[0] - left[1] < CONTAINER_GUTTER * height:
            continue
        before = set().union(*(s[2] for s in spans if s[1] <= left[1]))
        after = set().union(*(s[2] for s in spans if s[0] >= right[0]))
        if len(before) >= 2 and len(after) >= 2:
            return True
    return False


def _container_rows(cells, tops) -> set:
    """Rows covered by cells that hold a table of their own.

    Merged across the columns or not: a precinct bulletin ruled as four tall
    boxes, each a column of "NAME  PARTY  VOTES" lines, has no merged cell,
    and reading each box as one cell flattens forty rows into one.
    """
    rows: set = set()
    for cell in cells:
        if cell.lines < CONTAINER_LINES or not _has_gutter(cell.chars):
            continue
        span = max(1, _count(tops, cell.bbox[1], cell.bbox[3]))
        rows.update(range(cell.row, cell.row + span))
    return rows


def _segments(cells, tops, excluded) -> list:
    """Row ranges [start, end) of the separate tables inside one grid.

    Rows in `excluded` belong to no table and end the one above them.
    """
    by_row: dict = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)

    segments = []
    start = None
    edges: list = []
    reach = -1  # the last row any cell of this segment extends into
    for index in range(len(tops)):
        if index in excluded:
            if start is not None:
                segments.append((start, index))
                start = None
            continue
        if start is None:
            start, edges, reach = index, [], index

        row_cells = by_row.get(index, [])
        own = {c.bbox[0] for c in row_cells} | {c.bbox[2] for c in row_cells}
        new = [e for e in own if not any(abs(e - old) <= SNAP for old in edges)]
        crosses = any(
            c.bbox[0] + SNAP < e < c.bbox[2] - SNAP for c in row_cells for e in edges
        )
        # Never split where a merged cell from above reaches into this row.
        if index > start and new and crosses and reach < index:
            segments.append((start, index))
            start, edges = index, []
        edges.extend(own)
        for cell in row_cells:
            reach = max(reach, index + _count(tops, cell.bbox[1], cell.bbox[3]) - 1)

    if start is not None:
        segments.append((start, len(tops)))
    return segments


def _is_short(text: str) -> bool:
    flat = " ".join(text.split())
    return len(flat) <= FILL_DOWN_MAX_CHARS and len(flat.split()) <= FILL_DOWN_MAX_WORDS


def _read_segment(cells, tops, start, end, table_bottom) -> Grid | None:
    own = [cell for cell in cells if start <= cell.row < end]
    if not own:
        return None

    row_tops = tops[start:end]
    bottom = tops[end] if end < len(tops) else table_bottom
    columns = _snapped(cell.bbox[0] for cell in own)

    width = len(columns)
    height = len(row_tops)
    grid = [[None] * width for _ in range(height)]
    for cell in own:
        cell.col = max(0, bisect_right(columns, cell.bbox[0] + SNAP) - 1)
        cell.colspan = max(1, _count(columns, cell.bbox[0], cell.bbox[2]))
        cell.rowspan = max(1, _count(row_tops, cell.bbox[1], min(cell.bbox[3], bottom)))
        local_row = cell.row - start
        for r in range(local_row, min(local_row + cell.rowspan, height)):
            for c in range(cell.col, min(cell.col + cell.colspan, width)):
                grid[r][c] = cell

    result = Grid(
        bbox=(
            min(cell.bbox[0] for cell in own),
            row_tops[0],
            max(cell.bbox[2] for cell in own),
            max(min(cell.bbox[3], bottom) for cell in own),
        ),
        rows=[],
    )

    def starts_here(cell, r):
        return cell is not None and cell.row - start == r

    first = 0
    title = grid[0][0]
    if (
        width >= 2
        and height > 1
        and title is not None
        and all(cell is title for cell in grid[0])
        and title.rowspan == 1
    ):
        result.caption = " ".join(title.text.split())
        first = 1

    # A header split over rows: the first row has a cell merged down (the
    # "Course Code" beside "Periods/Week") and a cell merged across that the
    # next row subdivides (L, T, P under "Periods/Week").
    header_rows = 1
    if first < height:
        depth = max(
            (c.rowspan for c in grid[first] if starts_here(c, first)), default=1
        )
        if 2 <= depth <= 3 and first + depth <= height:
            subdivided = any(
                starts_here(grid[r][c], r)
                and grid[first][c] is not None
                and grid[first][c].colspan > 1
                for r in range(first + 1, first + depth)
                for c in range(width)
            )
            if subdivided:
                header_rows = depth

    body_from = first
    if header_rows > 1:
        combined = []
        for c in range(width):
            parts: list = []
            seen: set = set()
            for r in range(first, first + header_rows):
                cell = grid[r][c]
                if cell is None or id(cell) in seen:
                    continue
                seen.add(id(cell))
                text = " ".join(cell.text.split())
                if text and text not in parts:
                    parts.append(text)
            combined.append(" ".join(parts))
        result.rows.append(combined)
        result.header = True
        body_from = first + header_rows

    for r in range(body_from, height):
        if not any(starts_here(cell, r) for cell in grid[r]):
            # Every cell in this row continues one from above: it would only
            # repeat the row before it.
            continue
        row = []
        for c in range(width):
            cell = grid[r][c]
            if cell is None or c != cell.col:
                row.append("")
            elif starts_here(cell, r):
                row.append(cell.text)
            elif _is_short(cell.text):
                row.append(cell.text)
                result.filled_cells += 1
            else:
                row.append("")
        result.rows.append(row)

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
