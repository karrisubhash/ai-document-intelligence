"""Helpers for the quoting answerer: what a question asks for, what kind of
line a sentence is, and what a question names that the passages never mention.

The quoting answerer (llm.ExtractiveAnswerer) is what a reader gets whenever
no AI model can write the answer - at capacity, out of its free quota for the
day, down. It selects sentences by the words they share with the question. That
is all it can do, and measured against real documents with every model call
failing it made mistakes a word-overlap ranker makes, each of which looked to
the reader like an answer:

- It quoted a document TITLE or a clause HEADING ("FEE STRUCTURE 2024-25") on
  the strength of two shared words, because a heading is short and the
  tie-break prefers short.
- It answered one half of a two-part question: the coverage floor was measured
  over the terms of BOTH parts, so neither part's sentence cleared it alone.
- It skipped a table row whose question named a COLUMN ("tuition") that only
  the header row spells.
- It treated "B.Tech" and "M.Tech" as the same word (a one-letter token is
  dropped), so the wrong programme's row could come first.
- It dumped a whole fee table for a programme (PhD, B.Arch) the table does not
  have, and quoted the 2024-25 bus pass price for "2023-24".
- It quoted a sentence cut mid-word at a chunk boundary ("f member is entitled
  to 12 days...") and one cut off at its end.

Nothing here reads the passages semantically. Each helper is a narrow,
checkable rule, and the ones that REFUSE (names_what_the_passages_lack) refuse
only on something a reader can see for themselves: a code or a year in the
question that appears in no retrieved passage.
"""

import re

from . import facts as fact_forms
from .reranking import _terms

# A line that ends one of these is a sentence; one that does not and is short
# is a title, a clause heading or a list label.
SENTENCE_END = (".", "!", "?", ";")
MAX_HEADING_WORDS = 12

# A sentence this long that ends without punctuation was cut off by the end of
# its chunk, not written that way.
MIN_CUT_END_WORDS = 13

# What the question asks for. A figure or a date is the answer to "how much",
# "how long", "when", "by which date"; a sentence that has none cannot be it.
FIGURE_QUESTION = re.compile(
    r"\bhow\s+(?:much|many|long|often|far|old|soon|late)\b"
    r"|\b(?:fee|fees|fine|fines|cost|costs|price|charge|charges|penalty|limit"
    r"|minimum|maximum|percentage|percent|amount|rate|duration|period|number"
    r"|salary|stipend)\b",
    re.I,
)
DATE_QUESTION = re.compile(
    r"\bwhen\b|\bby\s+when\b|\bby\s+which\s+date\b|\bdeadline\b"
    r"|\bwhat\s+(?:\w+\s+){0,2}?(?:date|day|time|year|month)\b"
    r"|\bwhich\s+(?:\w+\s+){0,2}?(?:date|day|year|month)\b"
    r"|\blast\s+(?:date|day)\b",
    re.I,
)
SPELLED_NUMBER = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty"
    r"|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|crore)\b",
    re.I,
)

# A code like B.Tech, M.Tech, B.Arch, Ph.D: letters, a dot, letters. Matching
# drops one-letter words, so "B.Tech" and "M.Tech" were both just "tech".
DOTTED_CODE = re.compile(r"\b[A-Za-z]{1,6}(?:\.[A-Za-z]{1,10})+\b")
# Words that look like codes and are not.
NOT_A_CODE = frozenset({"eg", "ie", "vs", "etc", "dr", "mr", "mrs", "ms", "no", "st"})

# Identifiers a question can name: a dotted code, a mixed-case code (PhD) or an
# acronym of three letters or more (CSE, MBA). Two letters ("IT") is too short
# to tell from a paraphrase ("IT lab" for "computer lab").
MIXED_CASE = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b")
ACRONYM = re.compile(r"\b[A-Z]{3,}\b")

# A figure a reader can check: a year, a year range, a number of three or more
# digits. "2" and "75" turn up in most passages and prove nothing.
FIGURE = re.compile(r"\d{4}-\d{2,4}|\d[\d,]*")

# One- and two-letter words a chunk can start with. Anything else short at the
# start of a lowercase fragment is the end of a word the chunk boundary cut.
_SHORT_WORDS = frozenset(
    "a an as at be by do go he if in is it me my no of oh on or so to up us we".split()
)


def wants_figure(question: str) -> bool:
    """Whether the question asks for a figure or a date."""
    return bool(FIGURE_QUESTION.search(question) or DATE_QUESTION.search(question))


def wants_date(question: str) -> bool:
    """Whether the question asks for a date or a year."""
    return bool(DATE_QUESTION.search(question))


# A month in words, for "in July". "May" and "March" are left out: they are
# also a verb and a modal, and a sentence about what a student may do is not a
# sentence about a date.
MONTH_WORD = re.compile(
    r"\b(?:january|february|april|june|july|august|september|october|november"
    r"|december|jan|feb|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.I,
)


def has_figure(sentence: str) -> bool:
    return bool(
        re.search(r"\d", sentence)
        or SPELLED_NUMBER.search(sentence)
        or MONTH_WORD.search(sentence)
    )


# The "Clause 12.5", "Section 3" or "2." a heading starts with is a label, not
# a figure the heading states.
_LABEL = re.compile(
    r"^\s*(?:clause|section|article|rule|paragraph|para|chapter|annexure|appendix"
    r"|item|step)?\s*[A-Za-z]?\d+(?:\.\d+)*[.)]?\s*",
    re.I,
)


def heading_has_figure(sentence: str) -> bool:
    """Whether a heading states a figure or a date, not counting its own
    numbering."""
    return has_figure(_LABEL.sub("", sentence, count=1))


def code_terms(text: str) -> set:
    """Dotted codes as single terms: "B.Tech" is "btech", "M.Tech" is "mtech"."""
    codes = set()
    for match in DOTTED_CODE.finditer(text):
        letters = re.sub(r"[^a-z]", "", match.group(0).lower())
        if len(letters) >= 3 and letters not in NOT_A_CODE:
            codes.add(letters)
    return codes


def quote_terms(text: str) -> set:
    """The terms two texts are compared by: the shared content terms, and the
    dotted codes the shared definition cannot see."""
    return set(_terms(text)) | code_terms(text)


# What may follow a sentence's last full stop without changing how it ends: the
# quote or bracket that closes it.
_CLOSERS = ")]}\"'\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"


def _terminated(text: str) -> bool:
    """Whether the text ends the way a sentence does. A colon counts: "The
    following apply:" is an introduction, not a title."""
    core = text.rstrip().rstrip(_CLOSERS).rstrip()
    return core.endswith(SENTENCE_END + (":",))


def is_heading(sentence: str, *, is_row: bool) -> bool:
    """A title, a clause heading or a list label rather than a sentence.

    No full stop and a dozen words or fewer. A table row is never one, and a
    heading is not always useless ("Subject: Campus bus services for the
    academic year 2024-25" IS the answer to "which year"), so the answerer
    quotes one only when nothing else qualifies.
    """
    text = sentence.strip()
    if is_row or not text or _terminated(text):
        return False
    return len(text.split()) <= MAX_HEADING_WORDS


def trim_cut_start(sentence: str):
    """(sentence, whether it began mid-word or mid-sentence).

    A chunk boundary can fall inside a word: "f member is entitled..." is the
    end of "staff". A leftover of one or two letters that is not a word is
    dropped; the caller marks the start with an ellipsis, so the reader is told
    the sentence continues from before and the quote is still a substring of
    the passage.
    """
    text = sentence.lstrip()
    if not text or not text[0].islower():
        return sentence, False
    first, _, rest = text.partition(" ")
    if len(first) <= 2 and first not in _SHORT_WORDS and rest.strip():
        return rest.lstrip(), True
    return text, True


def ends_cut(sentence: str) -> bool:
    """A long sentence that stops without punctuation: its chunk ended there."""
    text = sentence.rstrip()
    return (
        bool(text)
        and not _terminated(text)
        and (len(text.split()) >= MIN_CUT_END_WORDS)
    )


def _identifier_tokens(question: str) -> set:
    found = set()
    # A question typed in capitals is shouting, not naming: every word of it
    # would be an "acronym". A dotted code is still a code.
    patterns = (
        (DOTTED_CODE,) if question.isupper() else (DOTTED_CODE, MIXED_CASE, ACRONYM)
    )
    for pattern in patterns:
        for match in pattern.finditer(question):
            letters = re.sub(r"[^a-z]", "", match.group(0).lower())
            if len(letters) >= 3 and letters not in NOT_A_CODE:
                found.add(letters)
    return found


def _letters_only(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def distinctive_figures(text: str) -> set:
    """The figures in `text` a reader could check against a document."""
    remainder, dates = fact_forms.extract_dates(fact_forms.prepare(text))
    figures = set(dates)
    for token in FIGURE.findall(remainder):
        normal = fact_forms.canonical_range(token.replace(",", "").strip("-"))
        if len(re.sub(r"\D", "", normal)) >= 3:
            figures.add(normal)
    return figures


def names_what_the_passages_lack(question: str, passages) -> str:
    """Why the passages cannot answer THIS question, or "" if they might.

    A word-overlap ranker has no way to notice that the question is about
    something else. Two things it can see for itself, and that a reader sees
    at once, are a CODE (PhD, B.Arch, CSE) or a FIGURE (a year, an amount) in
    the question that appears in no retrieved passage, title or column label:
    asked the tuition for a programme the fee table does not list, it quoted
    the table; asked the bus pass for 2023-24, it quoted the 2024-25 circular.
    Nothing is said about a word that is merely worded differently ("IT" for
    "computer", "complaint" for "grievance"): those stay with the overlap
    rules.
    """
    # Titles count as present here: this only decides whether to REFUSE, so
    # being generous about what a document mentions is the cautious direction.
    support = " ".join(
        f"{getattr(p, 'support_text', p.text)} {getattr(p, 'title_text', '')}"
        for p in passages
    )

    present = _letters_only(support)
    for token in sorted(_identifier_tokens(question)):
        if token not in present:
            return (
                f"the question names {token.upper()!r}, which no retrieved "
                f"passage, title or column mentions"
            )

    asked = distinctive_figures(question)
    if asked:
        known = distinctive_figures(support)
        known |= fact_forms.year_less(known)
        for figure in list(known):
            known |= fact_forms.range_years(figure)
        missing = sorted(figure for figure in asked if figure not in known)
        if missing:
            shown = ", ".join(
                f[len("date:") :] if f.startswith("date:") else f for f in missing
            )
            return f"the question names {shown}, which no retrieved passage mentions"

    return ""
