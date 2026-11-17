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
<th>, a chart's series, a ruled PDF's merged header) is used as it is. A
guessed one must read as column names - no figure, date, identifier, clause
or "label: value" in it, nothing repeated in its own column, no column under
it mixing names with numbers, and, if every column holds words, words that
name columns ("Name", "Description") or a row visibly unlike the rows below
it. Otherwise the rows are written without labels, every cell in its place
("23CS104; Workshop; ; ; 2"). A form - labels down a column with their values
beside them - is written as labels and values ("Roll No: 21A91A0501").

This module is pure (no Django) so the extractor, the chunker, the answerer
and the brief all read and write the one format.
"""

import re
from dataclasses import dataclass, field

from documents.processing.extractors.compose import (
    _DATE,
    _is_identifier,
    clean_cell,
    looks_like_value,
)
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

# How a table's rows are written.
LABELS = "labels"  # every value beside its column's name
FORM = "form"  # the first column names the value beside it; the rest as they are
FORM_PAIRS = "pairs"  # label | value | label | value: every pair named
PLAIN = "plain"  # values only, each in its place

# A figure in a two-column table's value column ("85000", "Rs. 1,200", "75%").
_FIGURE = re.compile(
    r"^[(+\-]?(?:rs\.?|inr|usd|eur|gbp|[$£€₹])?\s*\d[\d,]*(?:\.\d+)?%?\)?$",
    re.IGNORECASE,
)

# Words that name a column or a form's field. A first row is taken as a header
# only if most of its cells hold words like these, or if it is unlike the rows
# below it in ways measured below: "Asha Rao | Computer Science" names
# nobody's column, and one wrong label is worse than none.
_COLUMN_WORDS = frozenset(
    """
    name names no nos num number numbers id ids code codes title titles
    description descriptions desc details detail particulars remarks remark
    note notes comments comment type types category categories status date
    dates day days time timings timing year years semester sem month months
    period periods duration hours hrs hour course courses subject subjects
    paper papers unit units topic topics module modules chapter chapters
    credits credit marks mark grade grades point points score scores total
    amount amounts fee fees cost costs price prices rate rates qty quantity
    count department dept branch programme program programmes programs class
    classes section sections designation position role qualification
    experience email mail phone mobile contact address city state country
    place location venue room block hall faculty staff teacher instructor
    student students employee employees author authors publisher edition isbn
    book books reference references command commands syntax function purpose
    example examples value values parameter parameters item items activity
    activities event events outcome outcomes objective objectives result
    results percentage percent gender age father mother guardian dob birth
    roll reg registration admission ticket attendance signature week weeks
    start end deadline due mode method level size area capacity version step
    steps action actions owner responsibility batch slot serial sr balance
    debit voucher account head heads term terms rule rules clause clauses
    requirement requirements eligibility criteria minimum maximum min max
    column columns col header heading headings field fields company
    organisation organization institution college school university tuition
    hostel transport library lab laboratory medium religion caste
    nationality district mandal village pin pincode aadhaar aadhar bank
    cheque receipt invoice vendor supplier uom sanctioned released
    expenditure budget income expense expenses salary pay basic allowance
    deduction deductions net gross cgpa sgpa gpa cie see ese cia internal
    external practical theory tutorial co po pso intake strength seats
    vacancies applied admitted enrolled passed failed appeared sessional exam
    examination final mid assignment viva seminar elective electives stream
    tête colonne
    """.split()
)

# A single letter that heads a column in a syllabus: Lecture, Tutorial,
# Practical, Credits. Only when it is the whole cell: "S Reddy" is a name.
_SINGLE_LETTER_COLUMNS = frozenset("l t p c s".split())

# Words a section row inside a table is made of ("Practical", "Semester II").
# Not "Total" or "Note": a row of those is a row.
_SECTION_WORDS = frozenset(
    """
    semester sem year part section group unit module theory practical
    practicals lab labs laboratory elective electives core category stream
    track phase stage level term session batch audit project projects
    mandatory optional monday tuesday wednesday thursday friday saturday
    sunday department dept undergraduate postgraduate ug pg
    """.split()
)

# Words that make a capitalised lone row something other than a section.
_NOT_SECTION = frozenset(
    "total grand subtotal sub net note notes remark remarks signature date place".split()
)


@dataclass(frozen=True)
class Layout:
    """How a table's rows were written, so its next page writes alike.

    `labels` maps the extractor's column positions to their names, so a page
    whose block dropped a column that is empty there still labels the rest.
    """

    mode: str = PLAIN
    title: tuple = ()
    labels: tuple = ()  # ((column id, label), ...)
    sections: tuple = ()  # the section path open at the last row
    header: tuple = ()  # ((column id, cell), ...) of the header row, if any


@dataclass(frozen=True)
class Written:
    """One table as row lines, and what they cover."""

    lines: list
    rows: int
    labelled: bool
    columns: list = field(default_factory=list)
    layout: Layout = field(default_factory=Layout)

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
    # [(label, value)], label "" when the value has none. An unlabelled row
    # keeps its empty cells as ("", "") so every value stays in its column.
    pairs: list


# -- cells, labels, kinds ----------------------------------------------------


def _cells(rows) -> list:
    grid = [[clean_cell(cell) for cell in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return []
    width = max(len(row) for row in grid)
    return [row + [""] * (width - len(row)) for row in grid]


def _filled(row) -> list:
    return [index for index, cell in enumerate(row) if cell]


def _label(text: str) -> str:
    """A cell as a label: no trailing colon, and none of the separators the
    line is read back by ("Hours (L: T: P)" would end at "Hours (L")."""
    text = text.strip().rstrip(":").strip()
    return text.replace(LABEL_SEPARATOR, ":").replace(PAIR_SEPARATOR, ";")


_TOKEN = re.compile(r"[^\W\d_]+")


def _words(text: str) -> list:
    """The letters-only words of `text`, lower-cased ("Sl.No" is "sl", "no")."""
    return _TOKEN.findall(text.casefold())


def _vocabulary_hit(word: str) -> bool:
    if word in _COLUMN_WORDS:
        return True
    if len(word) > 3 and word.endswith("s") and word[:-1] in _COLUMN_WORDS:
        return True
    return len(word) > 4 and word.endswith("es") and word[:-2] in _COLUMN_WORDS


def _names_a_column(cell: str) -> bool:
    """Whether a cell is a column's name: a word that names a column in a
    cell of at most five words, or one of the single letters of a syllabus."""
    if cell.strip().casefold() in _SINGLE_LETTER_COLUMNS:
        return True
    words = _words(cell)
    return 0 < len(words) <= 5 and any(_vocabulary_hit(word) for word in words)


_TITLE_TOKEN = re.compile(r"^(?:[A-Z][a-z]+|[A-Z])\.?$")


def _name_like(cell: str) -> bool:
    """A person's name (or a place's): two to four words in Title Case, none
    of them a word that names a column, one of them a real word. "Ravi Kumar"
    heads no column - it is a value, and a first row holding one is data."""
    tokens = cell.split()
    if not 2 <= len(tokens) <= 4:
        return False
    if not all(_TITLE_TOKEN.match(token) for token in tokens):
        return False
    words = [token.strip(".").casefold() for token in tokens]
    for word in words:
        if _vocabulary_hit(word) or word in _FUNCTION_WORDS or word in _SECTION_WORDS:
            return False
    return any(len(word) >= 3 for word in words)


_NEUTRAL = re.compile(r"^(?:[^\w]*|[A-Za-z]{1,3}\.?)$")
_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)
# One token of letters and digits with a capital among them: "23CS1L1",
# "18CS42", "CO-2", "PSO1". Not "10cm/s", "20ns", "10-20m": those are measures.
_CODE_TOKEN = re.compile(
    r"^(?=[^\s]*[A-Z])(?=[^\s]*\d)[A-Za-z0-9][A-Za-z0-9./\-_]{2,23}$"
)


def _kind(cell: str) -> str:
    """ "figure", "date", "code" (a roll number, a course code) or "text"."""
    if _FIGURE.match(cell.replace(" ", "")):
        return "figure"
    words = cell.split()
    if len(words) <= 4 and _DATE.search(cell):
        return "date"
    if len(words) == 1 and (
        _is_identifier(cell) or (_CODE_TOKEN.match(cell) and not _ORDINAL.match(cell))
    ):
        return "code"
    return "text"


def _kinds(cells) -> list:
    """Kinds of the cells that say something: not empty, not "-" or "NA"."""
    return [_kind(cell) for cell in cells if cell and not _NEUTRAL.match(cell)]


# Brackets, the full-width ones too: a PDF can open with "（" and close
# with ")".
_OPENING = "([{（［｛"
_CLOSING = ")]}）］｝"
_BULLET = re.compile(r"^[•·▪◦‣○●■□➢➤✓✔*\-–]")


def _clean_label(text: str) -> bool:
    """A title, section or guessed column name must not be a fragment cut out
    of a line: no opening bracket left unclosed, none closed that was never
    opened, and not starting with a bracket or a bullet."""
    if _BULLET.match(text) or text[:1] in _OPENING:
        return False
    depth = 0
    for ch in text:
        if ch in _OPENING:
            depth += 1
        elif ch in _CLOSING:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _is_spanning_label(row, width: int) -> bool:
    """A row with one cell, in the first column, of a table at least three
    wide: a title above the table, a section inside it ("Practical"), or a
    note under it."""
    filled = _filled(row)
    if width < 3 or filled != [0]:
        return False
    text = row[0]
    return (
        bool(re.search(r"[^\W\d_]", text))
        and len(text.split()) <= MAX_SECTION_WORDS
        and not NUMERIC.match(text)
        and _clean_label(text)
    )


def _casing(cell: str) -> str:
    """How a cell's words are written: "T" Title Case, "U" capitals, "l" lower
    case - "B.Tech CSE" is "TU", "Ravi Kumar" "T", "CSE" "U"."""
    marks = set()
    for token in cell.split():
        letters = [ch for ch in token if ch.isalpha()]
        if not letters:
            continue
        if len(letters) >= 2 and all(ch.isupper() for ch in letters):
            marks.add("U")
        elif letters[0].isupper():
            marks.add("T")
        else:
            marks.add("l")
    return "".join(sorted(marks))


def _belongs_to_column(cell: str, others) -> bool:
    """Whether a lone first cell is a value of its column - "CS102" among
    course codes, "B.Tech ECE" among programmes, "Ravi Kumar" among names - and
    not a note, a total or a group heading in it. It must be of the kind of the
    cells above and below it; and a cell of words must be written like them and
    be no longer than they are. Then the row is a row with blanks, and its
    label is right; if not, the cell is written as it is, under no label."""
    others = [other for other in others if other]
    kinds = _kinds(others)
    if not kinds:
        return False
    dominant = max(set(kinds), key=kinds.count)
    if _kind(cell) != dominant:
        return False
    if dominant != "text":
        return True
    if _SENTENCE.search(cell) or _BULLET.match(cell) or cell[:1] in _OPENING:
        return False
    if any(word in _NOT_SECTION for word in _words(cell)):
        return False
    typical = _median([len(other.split()) for other in others])
    if len(cell.split()) > max(3.0, 1.5 * typical):
        return False
    styles = [_casing(other) for other in others]
    common = max(set(styles), key=styles.count)
    if styles.count(common) * 2 > len(styles) and _casing(cell) != common:
        return False
    return True


def _is_title(row, width: int) -> bool:
    """A spanning row at the top of a table that reads as its title: words,
    not a code or a figure, and not a sentence that ends with a full stop."""
    return (
        _is_spanning_label(row, width)
        and _kind(row[0]) == "text"
        and not row[0].rstrip().endswith((".", "!", "?"))
    )


# -- what kind of table ------------------------------------------------------

# Punctuation that ends or joins clauses: a cell with it is a piece of prose.
# A full stop ends a sentence after a word of three letters or more: "No. Of"
# and "Dr. Rao" are abbreviations.
_PROSE = re.compile(r"[,;，。；、]|(?<=[^\W\d_]{3})[.!?](?:\s|$)")
# The same, in any guessed column name: a sentence's full stop or a CJK
# clause mark, or a comma or semicolon at its end ("而网站，", "students,").
_SENTENCE = re.compile(r"[，。；]|[,;]$|(?<=[^\W\d_]{3})[.!?]\s")


def _mixed_column(row, body, *, share: int = 3) -> bool:
    """Whether some column below `row` mixes words with figures, dates or
    codes: at least 1/`share` of its cells each way (three cells at least).
    Rows that are a section label ("Practical") are not cells of a column."""
    width = len(row)
    cells_of = [
        other for other in body if not (width >= 3 and _is_spanning_label(other, width))
    ]
    for index in range(width):
        kinds = _kinds(other[index] for other in cells_of if index < len(other))
        if len(kinds) < 3:
            continue
        words = kinds.count("text")
        values = len(kinds) - words
        if words and values and min(words, values) * share >= len(kinds):
            return True
    return False


def _median(numbers) -> float:
    numbers = sorted(numbers)
    if not numbers:
        return 0.0
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return float(numbers[middle])
    return (numbers[middle - 1] + numbers[middle]) / 2


def _unlike_its_body(row, body) -> bool:
    """A first row plainly unlike the rows under it: no cell longer than its
    column usually is, and some column whose cells are much longer than its
    name ("Description" over sentences)."""
    longer = False
    for index, cell in enumerate(row):
        below = [len(other[index].split()) for other in body if other[index]]
        if not below:
            continue
        typical = _median(below)
        if len(cell.split()) > typical:
            return False
        if typical >= 2 * len(cell.split()) + 1:
            longer = True
    return longer


def _signature(cell: str) -> str:
    """A cell's shape: "Room 101" and "Room 102" are both "Aaaa 999"."""
    shape = re.sub(r"[0-9]", "9", cell)
    shape = re.sub(r"[A-Z]", "A", shape)
    return re.sub(r"[a-z]", "a", shape)


def _shaped_like_its_column(row, body) -> bool:
    """A first row whose cell with a number in it has the very shape of the
    cells below it ("Room 101" over "Room 102", "Lab 1" over "Lab 2") is the
    first row of data, not the name of the column."""
    for index, cell in enumerate(row):
        if not cell or not _has_digit(cell):
            continue
        below = [other[index] for other in body if other[index]]
        same = sum(_signature(other) == _signature(cell) for other in below)
        if below and same * 2 >= len(below):
            return True
    return False


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _digits_below(row, body) -> bool:
    """Labels over columns of figures ("GLONASS" over "10-20m", "Course
    Outcomes" over "CO-1", "2018 年 1-3 月" over amounts): of the columns
    whose first cell is words, at least half have digits in most cells below,
    and the one cell that is data in a row of names ("AB" over marks) does
    not count."""
    eligible = contrast = 0
    for index, cell in enumerate(row):
        below = [
            other[index]
            for other in body
            if other[index] and not _NEUTRAL.match(other[index])
        ]
        if not cell or _NEUTRAL.match(cell) or not below or _kind(cell) != "text":
            continue
        eligible += 1
        if sum(map(_has_digit, below)) * 2 > len(below):
            contrast += 1
    return contrast >= 1 and contrast * 2 >= eligible


def _capitals_above(row, body) -> bool:
    """A row in capitals over rows that are not ("PROFESSIONAL ELECTIVE |
    STREAM-1" over "Advanced Mechanics of Solids")."""
    cells = [cell for cell in row if cell]
    if not cells or not all(cell.isupper() for cell in cells):
        return False
    below = [cell for other in body for cell in other if cell]
    return bool(below) and sum(cell.isupper() for cell in below) * 2 <= len(below)


def _header_evidence(row, body) -> bool:
    """Positive evidence that a first row names the columns below it.

    A guess is not enough: the first row of a form ("Student | Ravi Kumar")
    or of a list ("Asha Rao | AB | AB") passes for a header on its shape alone,
    and one wrong label is worse than none. So a header must show one of:

    - words that name columns in at least two thirds of its cells (two cells
      must both): "Programme | Fee", "Command | Description | Syntax";
    - three or more short cells, half of them naming columns, over rows that
      have numbers where it has none (the detector's own rule);
    - three or more short cells in capitals over plain ones, or without digits
      over cells with digits, or short over sentences.
    """
    cells = [cell for cell in row if cell]
    named = sum(map(_names_a_column, cells))
    if named >= 2 and named * 3 >= len(cells) * 2:
        return True
    if len(cells) < 3:
        return False
    short = all(
        len(cell.split()) <= 4 and len(cell) <= 30 and not _PROSE.search(cell)
        for cell in cells
    )
    if not short:
        return False
    if named >= 2 and named * 2 >= len(cells) and guess_header([row, *body]):
        return True
    return (
        _capitals_above(row, body)
        or _digits_below(row, body)
        or _unlike_its_body(row, body)
    )


def _reads_as_header(row, body, *, strict: bool = False) -> bool:
    """Whether a guessed first row really names the columns below it.

    Column names are not values, names of people or clauses, are nearly all
    different, and do not turn up again in their own column: "Professor" in
    the first row of a staff list that has more professors below it is a row
    of data. Under real column names each column holds one kind of thing; a
    column of two names and a roll number is a form whose first row was
    guessed as its header ("Certified that | KARRI SUBHASH"). And it must
    show positive evidence (`_header_evidence`).

    `strict` is for a table read from layout alone (no ruling, no markup):
    every column with values in it must be named, other than the first.
    """
    labels = [cell for cell in row if cell]
    if len(labels) < 2:
        return False
    if any(looks_like_value(cell) or _SENTENCE.search(cell) for cell in labels):
        return False
    if any(_name_like(cell) for cell in labels):
        return False
    distinct = len({cell.casefold() for cell in labels})
    if (len(labels) - distinct) * 3 > len(labels):
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
    if _mixed_column(row, body):
        return False
    if _shaped_like_its_column(row, body):
        return False
    if strict and any(
        not row[index] and any(other[index] for other in body)
        for index in range(1, len(row))
    ):
        return False
    if strict and not all(_clean_label(cell) for cell in labels):
        return False
    return _header_evidence(row, body)


def _one_column_of_values(cells) -> bool:
    """Whether a two-column table's second column is one kind of value under
    a header that names it: figures (even one), or two or more dates or two or
    more codes. A column of mixed kinds - a name, a roll number, a date - is
    the values of a form, each named by its own row."""
    kinds = [_kind(cell) for cell in cells if cell]
    if not kinds or len(set(kinds)) != 1 or kinds[0] == "text":
        return False
    return kinds[0] == "figure" or len(kinds) >= 2


_FUNCTION_WORDS = frozenset(
    "a an the of and or to in on at for with by from as is are be".split()
)


def _label_like(key: str) -> bool:
    """Whether a cell reads as the name of the value beside it ("Roll No",
    "Date of birth", "CO-1"), rather than half of a line of text that runs
    across both columns ("relative positions of"), a bullet, or a figure."""
    words = key.split()
    if not words or len(words) > 6:
        return False
    if _BULLET.match(key) or NUMERIC.match(key) or looks_like_value(key):
        return False
    if sum(ch.isdigit() for ch in key) >= 2:
        return False
    if not (key[0].isupper() or key[0].isdigit()):
        return False
    return words[-1].casefold().strip(".,") not in _FUNCTION_WORDS


def _is_form(rows) -> bool:
    """Labels down the first column: in most rows that have a value beside
    it, the first cell reads as a label."""
    both = [row for row in rows if row[0] and row[1]]
    if not both:
        return False
    return sum(_label_like(row[0]) for row in both) * 5 >= len(both) * 3


def _form_values(rows) -> bool:
    """A form's second column holds values of different kinds - two or more
    names and at least one number, date or code - which no table column does.
    One word over a column of figures is a header, not a form."""
    kinds = _kinds(row[1] for row in rows)
    words = kinds.count("text")
    return words >= 2 and len(kinds) - words >= 1


def _colons(rows) -> bool:
    """Most first cells end with a colon ("Name:", "Roll No:")."""
    keys = [row[0] for row in rows if row[0]]
    return bool(keys) and sum(key.endswith(":") for key in keys) * 2 > len(keys)


def _form_keys_name_fields(rows) -> bool:
    """At least half of the first cells are names of fields ("Tuition Fee",
    "Date of Birth", "Roll No")."""
    keys = [row[0] for row in rows if row[0]]
    return bool(keys) and sum(map(_names_a_column, keys)) * 2 >= len(keys)


def _form_evidence(rows) -> bool:
    """Positive evidence that two columns are a form - labels with their
    values beside them - and not two columns of a table: the first cells read
    as labels AND they end with colons, or name fields, or the values beside
    them are of different kinds (names and a roll number and a date). Without
    it the rows are written as they are: "Year; Intake" claims nothing."""
    return _is_form(rows) and (
        _colons(rows) or _form_values(rows) or _form_keys_name_fields(rows)
    )


def _field_label(cell: str) -> bool:
    return _label_like(cell) and (cell.rstrip().endswith(":") or _names_a_column(cell))


def _paired_form(rows) -> bool:
    """label | value | label | value: in most rows every label position holds
    a field's name ("Name", "Father's Name", "Roll No", "Date of Birth") and
    the values beside them are mostly there."""
    width = len(rows[0])
    if width < 4 or width % 2 or len(rows) < 2:
        return False
    named = 0
    for row in rows:
        keys = [row[index] for index in range(0, width, 2) if row[index]]
        if keys and all(_field_label(key) for key in keys):
            named += 1
    values = [row[index] for row in rows for index in range(1, width, 2)]
    return named * 3 >= len(rows) * 2 and sum(map(bool, values)) * 2 >= len(values)


@dataclass
class _Plan:
    mode: str
    title: list
    labels: list  # per column of this grid, "" where none
    body: list
    header_row: list | None = None


def _plan(grid, header, header_declared: bool, strict: bool) -> _Plan:
    width = len(grid[0])

    # Titles: spanning rows at the very top, above at least one more row.
    title = []
    top = 0
    while top < MAX_TITLE_ROWS and len(grid) - top >= 2 and _is_title(grid[top], width):
        title.append(grid[top][0])
        top += 1
    rows = grid[top:]

    header = [clean_cell(cell) for cell in (header or [])]
    first = rows[0]
    body = rows[1:]
    blank = [""] * width

    def plan(mode, labels=None, body_rows=None, header_row=None):
        chosen = _Plan(mode, list(title), labels or blank, body_rows, header_row)
        return _demote_titles(chosen, grid[:top], width)

    def labelled():
        return plan(LABELS, [_label(cell) for cell in first], body, first)

    if header_declared and top == 0 and header == grid[0] and len(_filled(first)) >= 2:
        if body:
            return labelled()

    if width == 2:
        # A header over two columns, or a form of labels and values. Only
        # evidence picks either; otherwise the rows are written as they are.
        if body and _reads_as_header(first, body, strict=strict):
            return labelled()
        return plan(FORM if _form_evidence(rows) else PLAIN, body_rows=rows)

    # Forms before headers: "Name | KARRI SUBHASH | Father's Name | ..." has
    # a first row that would pass for column names.
    if _paired_form(rows):
        return plan(FORM_PAIRS, body_rows=rows)

    if body and _reads_as_header(first, body, strict=strict):
        return labelled()

    # A wider form: labels down the first column, and beside them values of
    # every kind - a name, a roll number - which no column of a table holds.
    if _is_form(rows) and (_form_values(rows) or _colons(rows)):
        return plan(FORM, body_rows=rows)
    return plan(PLAIN, body_rows=rows)


def _demote_titles(chosen: _Plan, title_rows: list, width: int) -> _Plan:
    """A title is a title only if nothing below it is a section of its own
    kind: in a day-wise timetable "Monday" at the top is the first section,
    and as a title it would stamp Monday on Tuesday's rows. "Semester I" over
    a table with a later "Practical" stays a title: they are not alike."""
    if not title_rows:
        return chosen
    kind = _family(title_rows[-1][0])
    later = [row[0] for row in chosen.body if _is_section(row, width)]
    if not any(_family(text) == kind for text in later):
        return chosen
    # The rows above are then the first section (or, in a table without
    # labels, rows), in their place before the rest.
    chosen.title = []
    chosen.body = [*title_rows, *chosen.body]
    return chosen


# Sections of one kind replace each other; a section of another kind nests.
_SECTION_FAMILIES = (
    frozenset(
        "theory practical practicals lab labs laboratory project projects".split()
    ),
    frozenset("elective electives core audit mandatory optional".split()),
    frozenset("monday tuesday wednesday thursday friday saturday sunday".split()),
)


def _family(text: str) -> str:
    """What kind of section a label is: "semester" for "Semester II", one
    family for Theory / Practical / Lab, one for the days of the week."""
    words = [word.strip(".:") for word in _words(text)]
    if not words:
        return ""
    for number, family in enumerate(_SECTION_FAMILIES):
        if words[0] in family:
            return f"family:{number}"
    return words[0] if len(words) > 1 else text.casefold()


def _next_sections(sections: list, label: str, after_data: bool) -> list:
    """The section path once `label` is read.

    Right after another section, it nests ("Semester I", then "Theory").
    After rows, it replaces the deepest section of its own kind and what was
    under it ("Practical" after "Semester I / Theory" is "Semester I /
    Practical"; "Semester II" is "Semester II"); a section of a kind not in
    the path starts it again.
    """
    if not after_data:
        return [*sections, label]
    kind = _family(label)
    for depth in range(len(sections) - 1, -1, -1):
        if _family(sections[depth]) == kind:
            return [*sections[:depth], label]
    return [label]


def _is_section(row, width: int) -> bool:
    """A lone-cell row of a labelled table that names the rows under it.

    Only on evidence that it is one: words that sections are made of
    ("Semester II", "Theory", "Monday", "Undergraduate"), words in capitals
    (not "TOTAL"), or a colon at its end. A lone code, a figure, a note, a
    "Total" is a row: stamped on the rows after it as their section, it would
    be a wrong statement about each of them.
    """
    if width < 2 or _filled(row) != [0]:
        return False
    text = row[0]
    if (
        not re.search(r"[^\W\d_]", text)
        or len(text.split()) > MAX_SECTION_WORDS
        or NUMERIC.match(text)
        or not _clean_label(text)
    ):
        return False
    if _kind(text) != "text":
        return False
    words = _words(text)
    if text.rstrip().endswith((":", "：")):
        return True
    if any(word in _NOT_SECTION for word in words):
        return False
    letters = sum(ch.isalpha() for ch in text)
    if text.isupper() and letters >= 4 and not _has_digit(text):
        return True
    return any(word in _SECTION_WORDS for word in words)


# -- writing ----------------------------------------------------------------


def _prefix(number: int, context, row: int) -> str:
    inner = CONTEXT_SEPARATOR.join(part for part in context if part)
    if inner:
        return f"Table {number} ({inner}), row {row}: "
    return f"Table {number}, row {row}: "


def _pairs(mode: str, row, labels) -> list:
    if mode == LABELS:
        return [(labels[index], cell) for index, cell in enumerate(row) if cell]
    if mode == FORM:
        return _pair(row[0], row[1]) + [("", cell) for cell in row[2:] if cell]
    if mode == FORM_PAIRS:
        out = []
        for index in range(0, len(row) - 1, 2):
            out += _pair(row[index], row[index + 1])
        if len(row) % 2 and row[-1]:
            out.append(("", row[-1]))
        return out
    # Plain: every cell in its place, empty ones too, so a value keeps its
    # column ("23CS104; Workshop; ; ; 2"); trailing blanks say nothing.
    cells = list(row)
    while cells and not cells[-1]:
        cells.pop()
    return [("", cell) for cell in cells]


def _pair(key: str, value: str) -> list:
    """One label and its value, from a form."""
    if not key:
        return [("", value)] if value else []
    if not value:
        return [("", key)]
    if not _label_like(key):
        # "1 | Introduction", "Intermediate (MPC) 94.5% | 2022 - 2024": this
        # row's first cell is not a label, so it is not written as one.
        return [("", key), ("", value)]
    return [(_label(key), value)]


def _render(pairs) -> str:
    return PAIR_SEPARATOR.join(
        f"{label}{LABEL_SEPARATOR}{value}" if label else value for label, value in pairs
    )


def _placed(grid, ids, all_ids) -> list:
    """`grid` with every cell at the position of its column among `all_ids`,
    so a value keeps its column from one page of a table to the next."""
    where = {column: position for position, column in enumerate(all_ids)}
    placed = []
    for row in grid:
        cells = [""] * len(all_ids)
        for index, cell in enumerate(row):
            cells[where[ids[index]]] = cell
        placed.append(cells)
    return placed


def write(
    number: int,
    rows,
    header=(),
    *,
    header_declared: bool = False,
    first_row: int = 1,
    strict: bool = False,
    column_ids=None,
    carry: Layout | None = None,
    plain: bool = False,
) -> Written:
    """Table `number`'s rows as labelled lines, numbered from `first_row`.

    `header` is what the extractor recorded (always its first row, or empty);
    `header_declared` says whether the source declared it, rather than it being
    guessed. `strict` asks more of a guessed header, for a table read from
    layout alone. `carry` is the layout of the page before, for the rest of a
    table that runs on (a long sheet): nothing is guessed again, its labels go
    to the columns `column_ids` names, and values of a table without labels
    are placed at their column's position. `plain` writes values only.

    No cell is lost. Empty cells are left out of a labelled row (the label
    says where a value belongs); a row that only repeats the header is not a
    row; a title or section is in the reference of every row it covers; a
    spanning row that is not a section (a note, a total, a legend, a lone
    value) is a row, written without a label.
    """
    grid = _cells(rows)
    if not grid:
        return Written(lines=[], rows=0, labelled=False)
    width = len(grid[0])
    ids = list(column_ids) if column_ids is not None else list(range(width))
    if len(ids) != width:
        ids = list(range(width))

    if carry is not None:
        by_id = dict(carry.labels)
        mode = carry.mode
        labels = [by_id.get(column, "") for column in ids]
        if mode != LABELS or not any(labels):
            mode = PLAIN
        printed = dict(carry.header)
        if mode == PLAIN:
            everything = sorted(set(by_id) | set(ids))
            grid = _placed(grid, ids, everything)
            ids = everything
            width = len(ids)
            labels = [""] * width
        # The header printed again at the top of this page is not a row.
        header_row = [printed.get(column, "") for column in ids] if printed else None
        chosen = _Plan(mode, list(carry.title), labels, grid, header_row)
        sections = list(carry.sections)
    elif plain:
        chosen = _Plan(PLAIN, [], [""] * width, grid)
        sections = []
    else:
        chosen = _plan(grid, header, header_declared, strict)
        sections = []

    body = chosen.body
    # In a table most of whose rows are one cell wide (and not sections)
    # there are no sections: it is a list down its first column.
    lone_rows = sum(
        1 for row in body if _filled(row) == [0] and not _is_section(row, width)
    )
    sections_allowed = chosen.mode == LABELS and lone_rows * 2 <= len(body)
    column_0 = [row[0] for row in body if _filled(row) != [0]]
    lines = []
    count = 0
    columns = []
    after_data = True

    for index, row in enumerate(body):
        if chosen.header_row is not None and row == chosen.header_row:
            # The header printed again, on a continuation page.
            continue
        lone = chosen.mode == LABELS and _filled(row) == [0]
        if (
            lone
            and sections_allowed
            and _is_section(row, width)
            and any(_filled(other) != [0] for other in body[index + 1 :])
        ):
            sections = _next_sections(sections, row[0], after_data)
            after_data = False
            continue
        # A row with only its first cell filled that is not a section: a
        # value with blanks beside it, or a note, a total, a legend. Under the
        # first column's label only if it is a value of that column; a note
        # would otherwise be a programme.
        note = lone and not _belongs_to_column(row[0], column_0)
        pairs = [("", row[0])] if note else _pairs(chosen.mode, row, chosen.labels)
        if not any(value for _, value in pairs):
            continue
        after_data = True
        # A note at the end of the table is about all of it, not about the
        # section it happens to follow.
        closing = note and not any(_filled(other) != [0] for other in body[index + 1 :])

        for label, _ in pairs:
            if label and label not in columns:
                columns.append(label)

        number_in_table = first_row + count
        count += 1
        context = [*chosen.title] if closing else [*chosen.title, *sections]
        lines.append(_prefix(number, context, number_in_table) + _render(pairs))

    layout = Layout(
        mode=chosen.mode,
        title=tuple(chosen.title),
        labels=tuple(
            (column, label) for column, label in zip(ids, chosen.labels, strict=False)
        ),
        sections=tuple(sections),
        header=tuple(
            (column, cell)
            for column, cell in zip(ids, chosen.header_row or (), strict=False)
        ),
    )
    return Written(
        lines=lines,
        rows=count,
        labelled=bool(columns),
        columns=columns,
        layout=layout,
    )


# -- reading ----------------------------------------------------------------


def parse_line(line: str) -> RowLine | None:
    """A row line read back, or None if `line` is not one."""
    match = ROW_PREFIX.match(line)
    if match is None:
        return None
    pieces = line[match.end() :].rstrip().split(PAIR_SEPARATOR)
    pairs = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            pairs.append(("", ""))
            continue
        label, separator, value = piece.partition(LABEL_SEPARATOR)
        if separator and value.strip() and label.strip():
            pairs.append((label.strip(), value.strip()))
        else:
            pairs.append(("", piece))
    while pairs and pairs[-1] == ("", ""):
        pairs.pop()
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


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _field_region(part: str) -> str:
    """One table's lines as a field matcher may read them.

    A line any of whose labels recurs in the table is blanked: in a table of
    many rows "Course Code: 23CS101" is one row's value, not the document's.
    The lines of a form name different fields and stay, with the reference
    ("Table 1, row 2: ") blanked and each "; " that parts two pairs turned
    into a line break of the same length, so a value ends where its cell
    ends and the next pair starts a line of its own.
    """
    lines = part.split("\n")
    parsed = [parse_line(line) for line in lines]
    counts = {}
    for row in parsed:
        if row is None:
            continue
        for label in {label for label, _ in row.pairs if label}:
            counts[label] = counts.get(label, 0) + 1

    out = []
    for line, row in zip(lines, parsed, strict=True):
        if row is None:
            out.append(line)
        elif any(counts.get(label, 0) > 1 for label, _ in row.pairs if label):
            out.append(_blank(line))
        else:
            head = ROW_PREFIX.match(line).end()
            out.append(_blank(line[:head]) + line[head:].replace(PAIR_SEPARATOR, "\n "))
    return "\n".join(out)


def field_view(text: str) -> str:
    """`text` as a field matcher may read it, offsets unchanged.

    The same length as `text`, character for character, like the view that
    turns cell pipes into spaces (normalize.matchable): a match in it is a
    span of the stored text, and what a match says is read from the stored
    text. See `_field_region` for what is blanked and what is kept.
    """
    spans = regions(text)
    if not spans:
        return text
    out = []
    cursor = 0
    for region in spans:
        out.append(text[cursor : region.start])
        out.append(_field_region(text[region.start : region.end]))
        cursor = region.end
    out.append(text[cursor:])
    return "".join(out)


def mask(text: str) -> str:
    """`text` with every row line blanked to spaces, offsets unchanged."""
    spans = regions(text)
    if not spans:
        return text
    out = []
    cursor = 0
    for region in spans:
        out.append(text[cursor : region.start])
        out.append(_blank(text[region.start : region.end]))
        cursor = region.end
    out.append(text[cursor:])
    return "".join(out)


def without_references(text: str) -> str:
    """`text` with each row's "Table N, row M:" reduced to its title and
    section, for counting what figures a passage supports: "row 3" is where a
    value is, not a value, and a "3 credits" it happened to match is not in
    the document."""
    return ROW_PREFIX.sub(lambda match: f"{match.group('context') or ''}: ", text)


# A table/row reference in an answer: "Table 2", "Table 2, row 3", "Table 3
# (Semester II), rows 4-6", "row 7", "rows 1 and 2", "rows 1, 3 and 5". Only
# the reference goes: what follows it ("row 2, 75 seats", "Table 2 (Rs. 95,000)")
# is the answer's, and its figures are checked. A list of rows is allowed a
# comma only when it ends with "and N", so "rows 1 and 2, 90 and 75" stops at 2.
_N = r"\d{1,4}"
_ROWS = (
    rf"(?:rows\s+(?:{_N}\s*(?:-|\u2013|\u2014|to)\s*{_N}"
    rf"|{_N}(?:\s*,\s*{_N})*\s+and\s+{_N}|{_N})|row\s+{_N})"
)
_ANSWER_REFERENCE = re.compile(
    rf"\bTable\s+\d{{1,5}}(?:\s*\([^)\n]{{0,200}}\))?\s*,?\s*{_ROWS}"
    rf"|\bTable\s+\d{{1,5}}"
    rf"|\b{_ROWS}",
    re.IGNORECASE,
)


def answer_without_references(text: str) -> str:
    """An answer with its table and row references taken out, so "Table 2,
    row 3" is not checked as if it stated the figures 2 and 3."""
    return _ANSWER_REFERENCE.sub(" ", text)


def reference(first_row: int, last_row: int, number: int) -> str:
    """ "Table 2, row 4" or "Table 2, rows 4-9", as a citation shows it."""
    if first_row == last_row:
        return f"Table {number}, row {first_row}"
    return f"Table {number}, rows {first_row}-{last_row}"
