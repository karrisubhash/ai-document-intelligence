"""Tables, as the question-answering path sees them.

A table chunk is a few dozen rows of cells with no sentence in them, and three
places in the RAG path were written for sentences. Each failed the same way: a
question about a table got "I couldn't find enough information" while the
table was in the corpus, was retrieved, and held the answer.

1. THE RERANKER SAW ROWS WITHOUT A SUBJECT. A cross-encoder judges whether a
   passage answers a question, and "Vikram Singh | Engineering | 9 | 12 | 22"
   says nothing about leave. Only the table's first chunk even carries its
   column labels, and no chunk carries its title. `scoring_text` adds both,
   for the reranker only: the stored chunk, and every citation of it, stay
   byte-identical to the page.

2. THE RELEVANCE FLOOR CANNOT JUDGE A QUESTION THAT COMPUTES. "Who has the
   highest earned leave?", "How many are in Engineering?" and "List the Legal
   staff" are not answered by any one row, so a cross-encoder scores the table
   0.00 - 0.14 against a floor of 0.30 (measured, 2026-11-14) and the engine
   abstains without ever showing the model the table. `admit_below_floor`
   lets a table through when something OTHER than the cross-encoder says it
   is on topic. The answerer's own refusal rule and the grounding check still
   stand behind it; what this removes is a gate that cannot see the question.

3. GROUNDING REFUSED EVERY FIGURE THAT IS NOT A CELL. A total is not written
   in the table, so "the Q1 sales for the North region total 15000" failed
   the check that every number an answer states appears in a cited passage.
   `derived_figures` computes the sums, counts, averages, highest and lowest
   values a table's own rows imply, so a correct total is accepted and a
   wrong one is still an invented figure.
"""

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from documents.processing.tables import text_rows

from .reranking import _terms

TABLE_KIND = "TABLE"

# Since ingestion-v4 a table is written as labelled rows ("Table 2, row 1:
# Course Code: 23CS101; Credits: 4"): every row carries its own column labels,
# so the header-in-metadata rules below matter only for the " | " rows of an
# earlier pass. Each rule reads both.
LABELLED = "labelled"


def is_labelled(chunk) -> bool:
    return (getattr(chunk, "metadata", None) or {}).get("layout") == LABELLED

# How many below-floor table passages may be shown to the model. Two is a
# table and its neighbour; more is the floor not being applied at all.
MAX_ADMITTED_TABLES = 2

# A table is admitted only if retrieval itself ranked it this high. Retrieval
# fuses lexical and vector similarity over the whole corpus, so a table in its
# top few is there for a reason the cross-encoder did not look at.
ADMIT_WITHIN_RETRIEVAL_RANK = 5

# Cell separators, in the order the detector tries them: " | " from the
# extractors that read real cells, tabs, then runs of spaces.
_SPLITTERS = (re.compile(r"\s*\|\s*"), re.compile(r"\t+"), re.compile(r" {2,}"))

_CURRENCY = re.compile(r"^(?:rs\.?|inr|usd|eur|gbp|[$€£₹])\s*", re.I)

# A table whose group columns or numbers multiply past this stops being
# enumerated. The cap bounds the work, not the correctness: a larger table is
# one the chunker already split.
MAX_DERIVED = 20_000


def is_table(chunk) -> bool:
    return (getattr(chunk, "metadata", None) or {}).get("kind") == TABLE_KIND


# --- 1. what the reranker reads -------------------------------------------


def scoring_text(chunk) -> str:
    """The text a reranker should judge for `chunk`.

    A prose chunk is judged as it is. A table chunk gets its document's title
    and its column labels in front, because rows alone do not say what they
    are rows of.
    """
    text = (chunk.text or "").strip()
    if not is_table(chunk) or not text:
        return text

    document = getattr(getattr(chunk, "document_version", None), "document", None)
    title = " ".join(str(getattr(document, "title", "") or "").split())
    if is_labelled(chunk):
        # The rows name their own columns; only the document is missing.
        return f"{title.rstrip('.')}.\n{text}" if title else text
    header = [str(label) for label in (chunk.metadata.get("header") or ()) if label]

    lead = title.rstrip(".") + ". " if title else ""
    if header:
        lead += "Table with columns: " + ", ".join(header) + "."
    elif title:
        lead += "Table."
    return f"{lead}\n{text}" if lead else text


# --- 2. which tables may skip the floor -----------------------------------


def _loose(terms) -> set:
    """Terms with trailing "e"s dropped, on top of reranking's stemming, which
    stems "employees" to "employe" but leaves "employee" whole - and a
    question naming a column in the plural is the usual one. All of them, so
    both sides land on "employ"."""
    return {t.rstrip("e") if len(t.rstrip("e")) >= 4 else t for t in terms}


def _label_terms(chunk) -> set:
    """Terms of what the table says it is: its document's title, its column
    labels and, for labelled rows, the table's own title and section."""
    document = getattr(getattr(chunk, "document_version", None), "document", None)
    metadata = chunk.metadata or {}
    labels = list(metadata.get("header") or ()) + list(metadata.get("columns") or ())
    if is_labelled(chunk) and not metadata.get("columns"):
        labels += text_rows.columns_of(chunk.text or "")
    header = " ".join(str(label) for label in labels)
    context = str(metadata.get("table_context") or "")
    title = str(getattr(document, "title", "") or "")
    return _loose(_terms(f"{title} {context} {header}"))


def table_matches_question(question: str, chunk) -> bool:
    """Whether the question is about this table, by evidence the cross-encoder
    was not asked for.

    A question that names the table's title or a column label is about it
    ("highest earned leave" against an Earned Leave column). A question that
    shares only cell values needs two of them ("Rahul Mehta"): a single shared
    word is a coincidence - "sales" is also a department in a staff table, and
    a question about Q3 sales matched it.
    """
    wanted = _loose(_terms(question))
    labelled = wanted & _label_terms(chunk)
    if labelled:
        return True
    return len(wanted & _loose(_terms(chunk.text or ""))) >= 2


def admit_below_floor(question, reranked, floor, retrieval_rank) -> list:
    """Table passages below `floor` that should still reach the model.

    `retrieval_rank` maps a chunk id to its 1-based place in retrieval.
    A table qualifies when retrieval ranked it near the top AND the question
    matches it (`table_matches_question`), so a table that merely sits in a
    corpus's nearest neighbours - "capital of France" against a fee table -
    does not. Best-scored first, at most MAX_ADMITTED_TABLES.

    Whether the reranker accepted something ELSE does not matter. A passage
    from another document - the staff leave RULES, for a question about
    earned leave - can pass the floor and say nothing about the table the
    question is about.
    """
    admitted = []
    for item in reranked:
        if item.score >= floor or not is_table(item.chunk):
            continue
        rank = retrieval_rank.get(item.chunk.id)
        if rank is None or rank > ADMIT_WITHIN_RETRIEVAL_RANK:
            continue
        if not table_matches_question(question, item.chunk):
            continue
        admitted.append(item)
        if len(admitted) >= MAX_ADMITTED_TABLES:
            break
    return admitted


# --- 3. figures a table implies -------------------------------------------


def parse_rows(text: str) -> list:
    """Rows of cells from a table chunk's text; lines that are not rows
    (a stray caption, a blank) are skipped."""
    rows = []
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        for splitter in _SPLITTERS:
            cells = [cell.strip() for cell in splitter.split(line.strip())]
            if len(cells) >= 2:
                rows.append(cells)
                break
    return rows


def parse_number(cell: str):
    """A cell's value as a Decimal, or None if it is not a plain figure.

    "85,000", "Rs. 1,200", "75%" and "-3.5" are figures; "B.Tech", "2024-25"
    and "12 Mar 2025" are labels, which must never be summed.
    """
    value = _CURRENCY.sub("", cell.strip())
    value = value.removesuffix("%").strip()
    if not re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", value):
        return None
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _forms(number: Decimal) -> set:
    """How a computed figure may be written: whole, or to 1 or 2 places.

    A number too large to round in the decimal context (27 digits or more:
    a serial or an account number in a numeric column) has no such forms and is
    skipped. It raised InvalidOperation, and a check that raises withholds every
    answer that cites the table.
    """
    forms = set()
    for places in (0, 1, 2):
        try:
            rounded = number.quantize(Decimal(1).scaleb(-places))
        except InvalidOperation:
            continue
        text = format(rounded, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        forms.add(text)
    return forms


def _aggregates(values: list) -> set:
    out = set()
    if not values:
        return out
    total = sum(values)
    out |= _forms(total)
    out |= _forms(max(values))
    out |= _forms(min(values))
    out |= _forms(total / len(values))
    return out


def labelled_row(line) -> dict:
    """A labelled row's cells keyed by column: the label, or the value's
    position when it has none (a second use of one label gets its position
    too, so neither value is lost)."""
    row = {}
    for position, (label, value) in enumerate(line.pairs):
        key = label if label and label not in row else f"#{position}"
        row[key] = value
    return row


def _table_groups(passages) -> list:
    """Each cited table's rows, with the parts of one table joined, so a total
    over two cited chunks of one table is checked against both.

    (rows, labelled) per table, each row a dict of cells keyed by column.
    Labelled rows have no header row among them; " | " rows may.
    """
    grouped = defaultdict(list)
    labelled = {}
    for passage in passages:
        chunk = passage.chunk
        if not is_table(chunk):
            continue
        key = (
            getattr(chunk, "document_version_id", None),
            chunk.metadata.get("table_index"),
        )
        lines = text_rows.row_lines(chunk.text) if is_labelled(chunk) else []
        if lines:
            labelled[key] = True
            grouped[key].extend(labelled_row(line) for line in lines)
            continue
        header = [str(label) for label in (chunk.metadata.get("header") or ())]
        for row in parse_rows(chunk.text):
            if header and row == header:
                continue
            grouped[key].append(dict(enumerate(row)))
    return [(rows, labelled.get(key, False)) for key, rows in grouped.items()]


def derived_figures(passages) -> set:
    """Figures the cited tables' own rows imply, as normalised fact tokens.

    Per column: the sum, average, highest and lowest value, and the row count.
    And the same per distinct label in every other column, so "total Q1 for
    North" and "how many in Engineering" are covered. Nothing else is
    computed - differences, percentages and ratios are still figures the
    answer must not invent.
    """
    figures = set()

    for rows, labelled in _table_groups(passages):
        if not rows:
            continue

        if not labelled:
            # A first row with no figure in it, above rows that have them, is
            # the header even when the extractor did not record one.
            has_figures = [
                any(parse_number(cell) is not None for cell in row.values())
                for row in rows
            ]
            if len(rows) > 1 and not has_figures[0] and any(has_figures[1:]):
                rows = rows[1:]
            elif not any(has_figures):
                # All text: the first row may or may not be a header, so
                # either count is an honest one.
                figures.add(str(len(rows) - 1))

        numeric = {}  # column -> [(row index, value)]
        labels = defaultdict(lambda: defaultdict(list))  # column -> label -> rows

        for index, row in enumerate(rows):
            for column, cell in row.items():
                number = parse_number(cell)
                if number is not None:
                    numeric.setdefault(column, []).append((index, number))
                elif cell:
                    labels[column][cell.lower()].append(index)

        figures.add(str(len(rows)))
        for entries in numeric.values():
            figures |= _aggregates([value for _, value in entries])

        for by_label in labels.values():
            if len(by_label) > 200:
                continue
            for member_rows in by_label.values():
                figures.add(str(len(member_rows)))
                members = set(member_rows)
                for entries in numeric.values():
                    values = [value for index, value in entries if index in members]
                    figures |= _aggregates(values)
            if len(figures) > MAX_DERIVED:
                return figures

    return figures


# --- 4. which rows a citation points at ------------------------------------

# A row is named as THE row a statement came from only if it shares one of
# these with the statement: a value with a letter and three characters, or a
# figure of three digits. "4" or "Yes" is in half the rows of any table.
_DISTINCT_LETTERS = 3
_DISTINCT_DIGITS = 3

# More rows than this fit the statement equally well: it is about the table,
# not about some rows of it, and the citation keeps the chunk's whole range.
MAX_NARROWED_ROWS = 3

_STATEMENT_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _statement_numbers(statement: str) -> set:
    numbers = set()
    for token in _STATEMENT_NUMBER.findall(statement):
        value = parse_number(token)
        if value is not None:
            numbers.add(value)
    return numbers


def _distinct(value: str) -> bool:
    if parse_number(value) is not None:
        return sum(ch.isdigit() for ch in value) >= _DISTINCT_DIGITS
    return len(value) >= _DISTINCT_LETTERS and bool(re.search(r"[^\W\d_]", value))


def _mentions(folded: str, numbers: set, value: str) -> bool:
    number = parse_number(value)
    if number is not None:
        return number in numbers
    wanted = " ".join(value.split()).casefold()
    if not wanted:
        return False
    return (
        re.search(rf"(?<![\w]){re.escape(wanted)}(?![\w])", folded) is not None
    )


def rows_supporting(text: str, statement: str) -> list:
    """The rows of `text` (labelled row lines) a statement was taken from.

    Each row is scored by the values it shares with the statement - distinct
    ones first, then all. The best-scoring rows are returned if at least one
    distinct value is shared and no more than MAX_NARROWED_ROWS rows tie;
    otherwise [] (the statement is about the table as a whole: a total, a
    count, the highest value).
    """
    lines = text_rows.row_lines(text)
    if not lines or not statement:
        return []
    folded = " ".join(statement.split()).casefold()
    numbers = _statement_numbers(statement)

    scored = []
    for line in lines:
        distinct = matched = 0
        for _, value in line.pairs:
            if _mentions(folded, numbers, value):
                matched += 1
                distinct += _distinct(value)
        scored.append(((distinct, matched), line.row))

    best = max(score for score, _ in scored)
    if best[0] < 1:
        return []
    rows = sorted({row for score, row in scored if score == best})
    return rows if len(rows) <= MAX_NARROWED_ROWS else []


def _label(number: int, rows: list) -> str:
    if len(rows) == 1:
        return f"Table {number}, row {rows[0]}"
    if rows == list(range(rows[0], rows[-1] + 1)):
        return f"Table {number}, rows {rows[0]}-{rows[-1]}"
    return f"Table {number}, rows " + ", ".join(str(row) for row in rows)


def table_reference(chunk, statement: str = "") -> dict | None:
    """Which table, and which of its rows, a cited chunk is.

    {"number": 2, "rows": [4, 5], "label": "Table 2, rows 4-5"} - the same
    reference the cited text opens each row with. With `statement` (the
    sentence of an answer that cites this chunk), narrowed to the rows that
    statement was taken from when that can be told; otherwise every row the
    chunk holds. None for anything but labelled table rows.
    """
    metadata = getattr(chunk, "metadata", None) or {}
    if metadata.get("layout") != LABELLED or not metadata.get("table_number"):
        return None
    number = int(metadata["table_number"])
    first = int(metadata.get("first_row") or 1)
    last = max(first, int(metadata.get("last_row") or first))

    rows = rows_supporting(chunk.text or "", statement) if statement else []
    narrowed = bool(rows)
    if not rows:
        rows = list(range(first, last + 1))

    reference = {
        "number": number,
        "first_row": rows[0],
        "last_row": rows[-1],
        "narrowed": narrowed,
        "label": _label(number, rows),
    }
    if len(rows) <= 200:
        reference["rows"] = rows
    if metadata.get("table_context"):
        reference["context"] = str(metadata["table_context"])
    return reference
