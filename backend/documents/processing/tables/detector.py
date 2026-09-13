"""Table detection over extracted page text (spec §11.3, §25 phase 12).

Pure functions over `(page_number, text)`, with no Django import, for the
same reason the chunk splitter and the field matcher are: the strategy can
be benchmarked offline against real documents without a database.

*Tables are detected in the stored page text, not by re-parsing the file.*

That is the decision the rest of this module follows from, and it is worth
being explicit about, because the obvious alternative - running a dedicated
PDF table parser over the original bytes - is what most systems do.

The reason is the span invariant. A table found in the page text has real
offsets into that text, so `page_text[start:end]` returns the table exactly
as stored, and every cell is a substring of the page. A cell the document
does not contain cannot be produced. A separate parser would emit cells with
no verifiable relationship to the text that every chunk, citation and
extracted field is already grounded in, and reconciling the two would be
guesswork dressed up as structure.

It also means one implementation covers a PDF text layer, an OCR pass, a
DOCX and a plain text file, because by this point they are all just text.

The cost is real, and stated rather than hidden: a borderless table that a
PDF text layer renders as ordinary prose is not detected. That is a miss,
and a miss is recoverable - the text is still chunked, still searchable and
still quotable. An invented row is not.
"""

import re
from dataclasses import dataclass, field

# A table needs at least this many rows and columns. One row is a heading;
# one column is a list. Neither is a table, and treating them as one fills
# the store with structure that answers nothing.
MIN_ROWS = 2
MIN_COLUMNS = 2

# A "cell" longer than this is a sentence that happened to contain the
# separator. Real cells are short: a label, a number, a date, a code.
MAX_CELL_LENGTH = 120

# A column gap has to be at least this wide, in every row, to count as a
# corridor. See _aligned - this is the check that separates a real column
# layout from prose that happens to contain a double space, and it is why
# the space separator is usable at all.
MIN_CORRIDOR_WIDTH = 2

# Ordered strongest first. A pipe or a tab is an explicit declaration of
# structure by whatever produced the text; runs of spaces are an accident of
# rendering that usually means the same thing - which is why only the last
# one has to pass the alignment check.
SEPARATORS = (
    ("PIPE", re.compile(r"\s*\|\s*")),
    ("TAB", re.compile(r"\t+")),
    ("SPACES", re.compile(r" {2,}")),
)

DETECTOR_NAME = "layout"
DETECTOR_VERSION = "v1"

# A cell that carries a number and no letters. Used only to tell a header
# row from a data row.
NUMERIC = re.compile(r"^[^A-Za-z]*\d[^A-Za-z]*$")


@dataclass(frozen=True)
class DetectedTable:
    """A table, and where in the page text it came from."""

    page_number: int
    table_index: int
    character_start: int
    character_end: int
    rows: list
    separator: str
    # Empty when the first row cannot be told apart from the data. See
    # _guess_header: a wrong header is worse than no header, because the
    # header is what gives a quoted number its meaning.
    header: list = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)


@dataclass(frozen=True)
class _Line:
    text: str
    start: int
    end: int


def _lines(text: str) -> list:
    """Lines with their offsets into the original string.

    Offsets are tracked while splitting rather than recovered afterwards
    with str.find, which would match the wrong occurrence of a line that
    appears twice - a table with two identical rows is entirely ordinary.
    """
    out = []
    position = 0
    for line in text.split("\n"):
        out.append(_Line(line, position, position + len(line)))
        position += len(line) + 1
    return out


def _split(line: str, pattern) -> list:
    cells = [" ".join(cell.split()) for cell in pattern.split(line.strip())]
    return [cell for cell in cells if cell != ""]


def _corridors(texts: list) -> int:
    """How many vertical whitespace channels run through every line.

    The first version of this compared the LEFT EDGE of each column and
    rejected almost every real table, because tables right-align their
    numbers under left-aligned headers:

        Course        Present   Total   Percent
        CS301              38      45      84.4

    "Present" starts at 14 and "38" at 18, so a left-edge test sees two
    unrelated lines. What actually holds - for left, right and centre
    alignment alike - is that the gap BETWEEN columns is blank in every
    row. Counting those corridors tests the thing that makes a layout a
    table, rather than one alignment convention out of three.

    A run of blank columns with no content to its right is trailing space,
    not a separator, so it does not count.
    """
    width = max(len(text) for text in texts)
    blank = [
        position
        for position in range(width)
        if all(position >= len(text) or text[position] == " " for text in texts)
    ]

    corridors = 0
    run_start = None
    for position in blank + [None]:
        if run_start is None:
            run_start, previous = position, position
            continue
        if position is not None and position == previous + 1:
            previous = position
            continue

        run_width = previous - run_start + 1
        has_content_right = any(len(text.rstrip()) > previous + 1 for text in texts)
        if run_start > 0 and run_width >= MIN_CORRIDOR_WIDTH and has_content_right:
            corridors += 1
        run_start, previous = position, position

    return corridors


def _aligned(lines: list, columns: int) -> bool:
    """Whether these lines really are laid out in columns.

    Every separator between columns must be a corridor that survives all
    the way down the run. Prose with a stray double space fails: its gaps
    fall wherever each sentence happened to break.
    """
    texts = [line.rstrip() for line in lines]
    return _corridors(texts) == columns - 1


def _usable(cells: list) -> bool:
    if len(cells) < MIN_COLUMNS:
        return False
    return all(len(cell) <= MAX_CELL_LENGTH for cell in cells)


def _guess_header(rows: list) -> list:
    """The first row, but only when it is clearly not data.

    The test is deliberately narrow: every cell of the first row must be
    non-numeric, and some later row must carry a number. A table of labels
    against labels gets no header, which is the honest answer - guessing
    would relabel somebody's data, and the header is exactly what an
    answer quotes to make a number mean something.
    """
    if len(rows) < 2:
        return []

    first = rows[0]
    if any(NUMERIC.match(cell) for cell in first):
        return []

    for row in rows[1:]:
        if any(NUMERIC.match(cell) for cell in row):
            return list(first)

    return []


def _runs(lines: list, name: str, pattern) -> list:
    """Maximal runs of consecutive lines with the same shape.

    Same separator, same column count, no blank line between. A row with a
    merged or wrapped cell breaks the run, so a table split that way is
    detected as its consistent parts, or not at all. Both are misses;
    neither invents a row that is not on the page.
    """
    found = []
    current = []
    width = None

    for line in lines:
        cells = _split(line.text, pattern) if line.text.strip() else []
        fits = _usable(cells) and (width is None or len(cells) == width)

        if fits:
            current.append(line)
            width = len(cells)
            continue

        if len(current) >= MIN_ROWS:
            found.append(list(current))
        current = []
        width = None

        # A line that is a usable row of a DIFFERENT width starts the next
        # run rather than being dropped with the boundary.
        if _usable(cells):
            current = [line]
            width = len(cells)

    if len(current) >= MIN_ROWS:
        found.append(current)

    if name == "SPACES":
        found = [
            run
            for run in found
            if _aligned(
                [line.text for line in run],
                len(_split(run[0].text, pattern)),
            )
        ]

    return found


def detect_tables(page_number: int, text: str) -> list:
    """Every table on one page, in reading order.

    Separators are tried strongest first, and a line already claimed by one
    table is never offered to another - so a pipe table whose cells contain
    double spaces is read once, as a pipe table.
    """
    if not text or not text.strip():
        return []

    lines = _lines(text)
    claimed = set()
    detected = []

    for name, pattern in SEPARATORS:
        # Runs have to be consecutive in the ORIGINAL text, so unclaimed
        # lines are grouped into blocks and each block scanned separately.
        # Concatenating them would let a run form across a gap that a
        # previous table occupies.
        blocks = []
        block = []
        for index, line in enumerate(lines):
            if index in claimed:
                if block:
                    blocks.append(block)
                    block = []
                continue
            block.append((index, line))
        if block:
            blocks.append(block)

        for block in blocks:
            for run in _runs([line for _, line in block], name, pattern):
                rows = [_split(line.text, pattern) for line in run]
                start, end = run[0].start, run[-1].end
                detected.append(
                    DetectedTable(
                        page_number=page_number,
                        table_index=0,  # assigned once every table is in
                        character_start=start,
                        character_end=end,
                        rows=rows,
                        separator=name,
                        header=_guess_header(rows),
                    )
                )
                for index, line in block:
                    if start <= line.start and line.end <= end:
                        claimed.add(index)

    detected.sort(key=lambda table: table.character_start)

    return [
        DetectedTable(
            page_number=table.page_number,
            table_index=order,
            character_start=table.character_start,
            character_end=table.character_end,
            rows=table.rows,
            separator=table.separator,
            header=table.header,
        )
        for order, table in enumerate(detected)
    ]


def verify(table: DetectedTable, page_text: str) -> bool:
    """Whether this table is genuinely present in the page text.

    Two conditions, both structural rather than statistical: the recorded
    span must re-slice out of the page, and every cell must appear inside
    that span. Together they make a fabricated cell impossible to store
    rather than merely unlikely - the same guarantee field extraction
    gives for a span.
    """
    if table.character_start < 0 or table.character_end > len(page_text):
        return False
    if table.character_start >= table.character_end:
        return False
    if not table.rows:
        return False

    region = page_text[table.character_start : table.character_end]
    normalized = " ".join(region.split())

    for row in table.rows:
        for cell in row:
            if cell and " ".join(cell.split()) not in normalized:
                return False
    return True


def render(table: DetectedTable) -> str:
    """A table as a pipe-delimited block, for a prompt or a preview.

    Never stored as the retrievable text: what gets indexed and quoted is
    the verbatim page region, so a citation points at something a reader
    can find on the page rather than at this reconstruction of it.
    """
    return "\n".join(" | ".join(row) for row in table.rows)
