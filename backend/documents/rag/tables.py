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

from .reranking import _terms

TABLE_KIND = "TABLE"

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
    """Terms of what the table says it is: its title and its column labels."""
    document = getattr(getattr(chunk, "document_version", None), "document", None)
    header = " ".join(str(label) for label in (chunk.metadata.get("header") or ()))
    title = str(getattr(document, "title", "") or "")
    return _loose(_terms(f"{title} {header}"))


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
    """How a computed figure may be written: whole, or to 1 or 2 places."""
    forms = set()
    for places in (0, 1, 2):
        rounded = number.quantize(Decimal(1).scaleb(-places))
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


def _table_groups(passages) -> list:
    """Each cited table's rows, with the parts of one table joined, so a total
    over two cited chunks of one table is checked against both."""
    grouped = defaultdict(list)
    for passage in passages:
        chunk = passage.chunk
        if not is_table(chunk):
            continue
        key = (
            getattr(chunk, "document_version_id", None),
            chunk.metadata.get("table_index"),
        )
        header = [str(label) for label in (chunk.metadata.get("header") or ())]
        for row in parse_rows(chunk.text):
            if header and row == header:
                continue
            grouped[key].append(row)
    return list(grouped.values())


def derived_figures(passages) -> set:
    """Figures the cited tables' own rows imply, as normalised fact tokens.

    Per column: the sum, average, highest and lowest value, and the row count.
    And the same per distinct label in every other column, so "total Q1 for
    North" and "how many in Engineering" are covered. Nothing else is
    computed - differences, percentages and ratios are still figures the
    answer must not invent.
    """
    figures = set()

    for rows in _table_groups(passages):
        if not rows:
            continue

        # A first row with no figure in it, above rows that have them, is the
        # header even when the extractor did not record one.
        has_figures = [
            any(parse_number(cell) is not None for cell in row) for row in rows
        ]
        if len(rows) > 1 and not has_figures[0] and any(has_figures[1:]):
            rows = rows[1:]
        elif not any(has_figures):
            # All text: the first row may or may not be a header, so either
            # count is an honest one.
            figures.add(str(len(rows) - 1))

        numeric = {}  # column -> [(row index, value)]
        labels = defaultdict(lambda: defaultdict(list))  # column -> label -> rows

        for index, row in enumerate(rows):
            for column, cell in enumerate(row):
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
