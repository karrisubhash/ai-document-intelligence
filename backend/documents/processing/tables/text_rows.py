"""Tables written as text: one labelled line per row.

A table is not stored as a grid. Every row becomes one line of the page text,
each value next to the name of its column, and the line opens with the table's
number in the document and the row's number in the table:

    Table 2, row 1: Course Code: 23CS101; Course Title: Engineering Mathematics; Credits: 4
    Table 2, row 2: Course Code: 23CS102; Course Title: Applied Physics; Credits: 3

A table with a title (a first row that spans the table: "IIIrd Year - Semester
II") or a section row inside it ("Practical") says so in every row it covers:

    Table 3 (IIIrd Year - Semester II / Practical), row 7: Course Code: ...

Why text, and why every row carries its own labels and reference:

*A row means something on its own.* A long table is cut into several chunks.
Written as cells, only the chunk with the header row knew what its numbers
were; "85000" in the third chunk was a number and nothing more. Written as
labelled rows, every chunk - and every line a quote is taken from - says what
each value is, which table it belongs to and which row it is.

*The reference is valid by construction.* "Table 2, row 1" is part of the
stored text, so a citation of a chunk shows it, the Text tab shows it at the
cited offsets, and nothing has to be looked up in a second store that could
disagree with the text. The values are the cells as the extractor read them
(verified against the page before they were written here); only the
separators and the labels around them are added.

*Headers are not guessed into labels.* A wrong label is worse than none: it
says a number is something it is not. A header the source declared (an HTML
<th>, a spreadsheet's column names, a chart's series, a ruled PDF's merged
header) is used as it is. A guessed one must read as column names - no figure,
date, identifier or "label: value" in it - or the row is written without
labels. A two-column table with no such header is a list of labels and their
values ("Roll No: 21A91A0501"), which is how it is written.

This module is pure (no Django) so the extractor, the chunker, the answerer
and the brief all read and write the one format.
"""

import re
from dataclasses import dataclass, field

from documents.processing.extractors.compose import clean_cell, looks_like_value
from documents.processing.tables.detector import NUMERIC, guess_header

PAIR_SEPARATOR = "; "
LABEL_SEPARATOR = ": "
CONTEXT_SEPARATOR = " / "

# The opening of a row line. Anchored at a line start; the table number and
# row number are bounded so a stray "Table 2024, row 99999999" in prose is
# not taken for one.
ROW_PREFIX = re.compile(
    r"^Table (?P<number>[1-9]\d{0,4})(?: \((?P<context>[^\n]*?)\))?, "
    r"row (?P<row>[1-9]\d{0,6}): ",
    re.MULTILINE,
)

# Title and section rows: at most this many title rows above a table, and a
# section label longer than this many words is a sentence, not a label.
MAX_TITLE_ROWS = 2
MAX_SECTION_WORDS = 10

# A figure in a two-column table's value column ("85000", "Rs. 1,200", "75%").
_FIGURE = re.compile(
    r"^[(+\-]?(?:rs\.?|inr|usd|eur|gbp|[$£€₹])?\s*\d[\d,]*(?:\.\d+)?%?\)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Written:
    """One table as row lines, and what they cover."""

    lines: list
    rows: int
    labelled: bool
    columns: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class Region:
    """A run of row lines of one table in a page's text."""

    number: int
    start: int
    end: int
    first_row: int
    last_row: int
    context: str = ""


@dataclass(frozen=True)
class RowLine:
    number: int
    row: int
    context: str
    pairs: list  # [(label, value)], label "" when the value has none


# -- writing ----------------------------------------------------------------


def _cells(rows) -> list:
    grid = [[clean_cell(cell) for cell in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return []
    width = max(len(row) for row in grid)
    return [row + [""] * (width - len(row)) for row in grid]


def _filled(row) -> list:
    return [index for index, cell in enumerate(row) if cell]


def _is_spanning_label(row, width: int) -> bool:
    """A row with one cell, in the first column, of a table at least three
    wide: a title above the table or a section inside it ("Practical")."""
    filled = _filled(row)
    if width < 3 or filled != [0]:
        return False
    text = row[0]
    return (
        bool(re.search(r"[^\W\d_]", text))
        and len(text.split()) <= MAX_SECTION_WORDS
        and not NUMERIC.match(text)
    )


def _label(text: str) -> str:
    return text.strip().rstrip(":").strip()


def _reads_as_header(row, body) -> bool:
    """Whether a guessed first row really names the columns below it.

    Column names are not values (no figure, date, identifier or "label:
    value"), are all different, and do not turn up again in their own column:
    "Professor" in the first row of a staff list that has more professors
    below it is a row of data, not the name of a column.
    """
    labels = [cell for cell in row if cell]
    if len(labels) < 2:
        return False
    if any(looks_like_value(cell) for cell in labels):
        return False
    if len({cell.casefold() for cell in labels}) != len(labels):
        return False
    for index, cell in enumerate(row):
        if not cell:
            continue
        wanted = cell.casefold()
        for other in body:
            if other == row:
                # The header printed again (a continuation page): still a header.
                continue
            if index < len(other) and other[index].casefold() == wanted:
                return False
    return bool(guess_header([row, *body])) or _all_text_header(row, body)


def _all_text_header(row, body) -> bool:
    """A header over text columns, which the narrow figure-based guess cannot
    see: three or more columns, every one named, and a body below it."""
    return len(row) >= 3 and all(row) and len(body) >= 1


def _figures(cells) -> bool:
    values = [cell for cell in cells if cell]
    return bool(values) and all(_FIGURE.match(cell.replace(" ", "")) for cell in values)


@dataclass
class _Plan:
    title: list
    labels: list | None
    pairs: bool
    body: list
    # The header row as it appears in the grid, to recognise it printed again.
    header_row: list | None = None


def _plan(grid, header, header_declared: bool) -> _Plan:
    width = len(grid[0])

    # Titles: spanning rows at the very top, above at least one more row.
    title = []
    top = 0
    while (
        top < MAX_TITLE_ROWS
        and len(grid) - top >= 2
        and _is_spanning_label(grid[top], width)
    ):
        title.append(grid[top][0])
        top += 1
    rows = grid[top:]

    header = [clean_cell(cell) for cell in (header or [])]
    first = rows[0]
    body = rows[1:]

    def labelled():
        return _Plan(title, [_label(cell) for cell in first], False, body, first)

    declared = (
        header_declared
        and top == 0
        and header == grid[0]
        and len(_filled(first)) >= 2
    )
    if declared and body:
        return labelled()

    if width == 2:
        # A label and its value, unless the second column is a column of
        # figures under a header that names it ("Programme | Fee").
        if body and _reads_as_header(first, body) and _figures(r[1] for r in body):
            return labelled()
        return _Plan(title, None, True, rows)

    # A header the source did not declare must read as one. What the
    # extractor guessed is not trusted on its own: under a title, its guess
    # was the title.
    if body and _reads_as_header(first, body):
        return labelled()

    return _Plan(title, None, False, rows)


def _pair(key: str, value: str) -> list:
    """One row of a two-column table of labels and values."""
    if not key:
        return [("", value)] if value else []
    if not value:
        return [("", key)]
    if NUMERIC.match(key) or looks_like_value(key):
        # "1 | Introduction", "2024-25 | 85000": the first column is not a
        # label, so it is not written as one.
        return [("", key), ("", value)]
    return [(_label(key), value)]


def _prefix(number: int, context: list, row: int) -> str:
    inner = CONTEXT_SEPARATOR.join(part for part in context if part)
    if inner:
        return f"Table {number} ({inner}), row {row}: "
    return f"Table {number}, row {row}: "


def write(
    number: int,
    rows,
    header=(),
    *,
    header_declared: bool = False,
    first_row: int = 1,
) -> Written:
    """Table `number`'s rows as labelled lines, numbered from `first_row`.

    `header` is what the extractor recorded (always its first row, or empty);
    `header_declared` says whether the source declared it, rather than it being
    guessed. Empty cells are left out of a row; a row that only repeats the
    header (a header printed again on a continuation page) is not a row.
    """
    grid = _cells(rows)
    if not grid:
        return Written(lines=[], rows=0, labelled=False)

    plan = _plan(grid, header, header_declared)
    width = len(grid[0])
    section = ""
    lines = []
    count = 0
    columns = []

    for row in plan.body:
        if plan.header_row is not None and row == plan.header_row:
            # The header printed again, on a continuation page.
            continue
        if _is_spanning_label(row, width):
            # A section inside the table: it names the rows under it.
            section = row[0]
            continue

        if plan.pairs:
            pairs = _pair(row[0], row[1])
        elif plan.labels is not None:
            pairs = [
                (plan.labels[index], cell) for index, cell in enumerate(row) if cell
            ]
        else:
            pairs = [("", cell) for cell in row if cell]
        if not pairs:
            continue

        for label, _ in pairs:
            if label and label not in columns:
                columns.append(label)

        number_in_table = first_row + count
        count += 1
        body = PAIR_SEPARATOR.join(
            f"{label}{LABEL_SEPARATOR}{value}" if label else value
            for label, value in pairs
        )
        lines.append(_prefix(number, [*plan.title, section], number_in_table) + body)

    return Written(
        lines=lines,
        rows=count,
        labelled=plan.labels is not None or plan.pairs,
        columns=columns,
    )


# -- reading ----------------------------------------------------------------


def parse_line(line: str) -> RowLine | None:
    """A row line read back, or None if `line` is not one."""
    match = ROW_PREFIX.match(line)
    if match is None:
        return None
    pairs = []
    for piece in line[match.end() :].split(PAIR_SEPARATOR):
        piece = piece.strip()
        if not piece:
            continue
        label, separator, value = piece.partition(LABEL_SEPARATOR)
        if separator and value.strip() and label.strip():
            pairs.append((label.strip(), value.strip()))
        else:
            pairs.append(("", piece))
    return RowLine(
        number=int(match.group("number")),
        row=int(match.group("row")),
        context=match.group("context") or "",
        pairs=pairs,
    )


def row_lines(text: str) -> list:
    """Every row line in `text`, in order."""
    out = []
    for line in (text or "").split("\n"):
        parsed = parse_line(line.strip())
        if parsed is not None:
            out.append(parsed)
    return out


def regions(text: str) -> list:
    """Runs of consecutive row lines of one table, with their spans.

    A run ends at a line that is not a row line, or at a row line of another
    table. Spans index `text` exactly and cover whole lines.
    """
    found = []
    current = None
    position = 0

    for line in (text or "").split("\n"):
        start, end = position, position + len(line)
        position = end + 1

        parsed = parse_line(line) if line.startswith("Table ") else None
        if parsed is None:
            if current is not None:
                found.append(current)
                current = None
            continue

        if current is not None and current.number == parsed.number:
            current = Region(
                number=current.number,
                start=current.start,
                end=end,
                first_row=current.first_row,
                last_row=parsed.row,
                context=current.context,
            )
            continue

        if current is not None:
            found.append(current)
        current = Region(
            number=parsed.number,
            start=start,
            end=end,
            first_row=parsed.row,
            last_row=parsed.row,
            context=parsed.context,
        )

    if current is not None:
        found.append(current)
    return found


def columns_of(text: str) -> list:
    """The labels used in `text`'s row lines, in first-seen order."""
    seen = []
    for line in row_lines(text):
        for label, _ in line.pairs:
            if label and label not in seen:
                seen.append(label)
    return seen


def mask(text: str) -> str:
    """`text` with every row line blanked to spaces, offsets unchanged.

    For readers that look for one value of the whole document ("Course Code:
    ...") and must not take it from one row of a table of many.
    """
    spans = regions(text)
    if not spans:
        return text
    out = []
    cursor = 0
    for region in spans:
        out.append(text[cursor : region.start])
        out.append(re.sub(r"[^\n]", " ", text[region.start : region.end]))
        cursor = region.end
    out.append(text[cursor:])
    return "".join(out)


def reference(first_row: int, last_row: int, number: int) -> str:
    """"Table 2, row 4" or "Table 2, rows 4-9", as a citation shows it."""
    if first_row == last_row:
        return f"Table {number}, row {first_row}"
    return f"Table {number}, rows {first_row}-{last_row}"
