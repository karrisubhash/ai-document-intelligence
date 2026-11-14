"""Grounding and citation validation.

An answer is not trusted because a model produced it. Before anything is
returned, four questions get asked of it:

1. Does every marker it cites exist in the context it was given? A citation
   to [7] when six passages were supplied is a fabricated reference, and it
   is the single cheapest hallucination to detect.
2. Did it cite anything at all? A substantive claim with no citation cannot
   be checked by the person reading it.
3. Does every FACT it states appear in the passages it cited? Numbers,
   percentages, money, dates and identifiers are the claims a reader acts
   on, and a wrong one is the expensive failure this system exists to
   prevent. This is a hard gate.
4. Is the prose as a whole drawn from those passages? Approximate by
   construction, so it is a loose backstop rather than the primary test.

A failure downgrades the answer to an abstention. Section 12.4 is explicit
that abstaining beats inventing.

WHY THIS WAS REWRITTEN, because the previous design failed in a way worth
recording. It had only checks 1, 2 and 4, and check 4 was the gate: at
least 55% of an answer's content words had to appear in the cited passages.
Measured against real answers over real passages, that produced:

    terse extractive quote          support 1.000   PASS
    close paraphrase, 2 sentences   support 0.647   PASS
    structured answer with headings support 0.456   REJECTED
    conversational answer           support 0.298   REJECTED

Both rejected answers were factually correct and fully cited. What sank
them was "answer", "detail", "deadline", "essentially", "before" - English
connective tissue. A bag-of-words overlap ratio cannot tell a fabricated
claim from a well-written sentence, because it is not measuring truth; it
is measuring how closely the prose echoes the source's vocabulary. As a
gate it therefore selects for answers that read like search results, and
rejects the ones that read like an explanation - while a fluent answer that
quietly changed 75 to 80 sails through, because "80" is one token among
sixty and the ratio barely moves.

So the gate moved to where invention actually shows up. Check 3 is blind to
style - "Great question! You'll need 75% [1]" and "The minimum is 75
percent [1]" carry the same facts - and unforgiving about substance: a
figure that is in no cited passage fails, however fluently it is written.
Check 4 survives at a much lower threshold, catching only wholesale
invention (an answer that cites a passage and then discusses something
else), and reports its ratio either way so the signal is still measurable.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings

from . import facts as fact_forms
from . import tables
from .llm import is_abstention
from .reranking import _terms

MARKER = re.compile(r"\[(\d{1,3})\]")

# Share of an answer's content words that must appear in the cited
# passages. This is the BACKSTOP, not the gate - see the module docstring.
# Set low enough that any legitimately-written answer passes and only
# wholesale invention fails: an answer that cites [1] and then discusses a
# subject [1] does not mention. Raising it back toward 0.55 re-creates the
# style filter this replaced.
MIN_SUPPORT_RATIO = 0.25

# Tokens that carry a fact a reader will act on. Deliberately narrow: the
# aim is the claims where being wrong is expensive and being approximate is
# not acceptable, not every noun in the answer.
#
#   75%  85,000  1,00,000  3.5  2024  REG/2024/117  section 4.2  $1,200
#
# Anything containing a digit qualifies. Words do not: "seven days" is
# covered by the prose backstop, and adding number-words here would mean
# maintaining a spelled-out-numeral table to catch a case that check 4
# already handles.
FACT = re.compile(r"[A-Za-z]*\d[\w./,\-:]*")

# Markdown ordinals - "1." or "2)" opening a line - are formatting, not
# claims. Without this, every numbered list in an answer contributes facts
# that are not in any document and the answer is rejected for being tidy.
ORDINAL_LINE = re.compile(r"^\s*(?:[-*]\s*)?(\d{1,2})\s*[.)]\s+", re.M)

# A comma between two digits is a thousands separator, not a word boundary.
# It has to be removed before the PROSE check as well as the fact check,
# because `_terms` splits on non-word characters and would otherwise turn
# one figure into two meaningless tokens:
#
#   answer   "Tuition is 85,000 per year. [1]"  -> ... "85", "000" ...
#   passage  "The annual tuition ... is 85000."  -> ... "85000" ...
#
# Neither token matches, so an answer that quotes the document's own figure
# correctly scores as unsupported. Caught by
# FactGroundingTests.test_thousands_separators_do_not_invent_a_fact, where
# the fact check passed and the backstop failed the same sentence.
THOUSANDS = re.compile(r"(?<=\d),(?=\d)")


def _prose_terms(text: str) -> list:
    """Content terms for the support ratio, with figures left intact."""
    return _terms(THOUSANDS.sub("", fact_forms.prose_form(text)))


@dataclass
class GroundingReport:
    grounded: bool = True
    cited_markers: tuple = ()
    invalid_markers: tuple = ()
    support_ratio: float = 0.0
    unsupported_terms: tuple = ()
    # Facts asserted by the answer that appear in none of the passages it
    # cited. This is the finding that matters: each entry is a number the
    # answer stated and the documents did not.
    unsupported_facts: tuple = ()
    failures: list = field(default_factory=list)
    # The answer as it should be shown: citations written as a group
    # ("[1, 2]") split into one marker each. Empty until validate() has run.
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.grounded and not self.failures


def extract_markers(text: str) -> tuple:
    """Markers the answer cites, in the order they first appear."""
    seen = []
    for match in MARKER.finditer(fact_forms.normalize_markers(text)):
        value = int(match.group(1))
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def _normalize_fact(token: str) -> str:
    """A fact's comparable form.

    The same figure is written many ways across a corpus and an answer, and
    a check that missed those equivalences would reject correct answers for
    formatting:

        "75%"      "75 percent"   "75"        -> 75
        "85,000"   "85000"                    -> 85000
        "4.2."     "4.2"                      -> 4.2

    Separators inside the token are dropped, trailing sentence punctuation
    is stripped, and letters are lowered so "Rs1200" and "rs1200" agree.
    Identifiers keep their internal structure ("reg/2024/117") because
    there the structure IS the fact.
    """
    token = token.strip().strip(".,;:").lower()
    # Thousands separators only - a comma between digits. A comma elsewhere
    # is part of an identifier and stays.
    token = re.sub(r"(?<=\d),(?=\d)", "", token)
    # A bare trailing separator left by the strip above.
    token = token.rstrip("./-:")
    # "2024-25" is "2024-2025".
    return fact_forms.canonical_range(token)


# A figure with a unit or currency stuck to it ("rs1200", "10km", "75%"),
# and a whole-hour time ("10:00"). The fact tokenizer keeps attached letters
# and the minutes, so "Rs1200" in a passage and "Rs 1200" or "1,200 rupees"
# in the answer compared as "rs1200" against "1200", and "10:00 p.m."
# against "10 PM" as "10:00" against "10" - correct answers rejected for
# formatting, which _normalize_fact exists to prevent. Identifiers with
# internal structure ("reg/2024/117") match neither pattern and still have
# to match exactly.
UNIT_FIGURE = re.compile(r"[a-z]*?(\d+(?:\.\d+)?)[a-z%]*")
WHOLE_HOUR = re.compile(r"(\d{1,2}):00")


def _numeric_core(fact: str):
    """The bare number in a figure, or None for anything more structured."""
    match = UNIT_FIGURE.fullmatch(fact) or WHOLE_HOUR.fullmatch(fact)
    return match.group(1) if match else None


def _facts(text: str) -> set:
    """Fact tokens in a piece of text, normalized."""
    return {
        normalized for raw in FACT.findall(text) if (normalized := _normalize_fact(raw))
    }


def _answer_facts(answer_text: str) -> set:
    """Facts the answer asserts, excluding its own formatting.

    Citation markers are removed first - "[1]" is a reference, not a claim
    that the number one appears in the document - and so are markdown list
    ordinals, which are numbering rather than content.
    """
    stripped = MARKER.sub(" ", answer_text)
    stripped = ORDINAL_LINE.sub(" ", stripped)
    # A date the answer states is ONE fact, compared whole against the dates
    # the passages state: its day and its year are not facts of their own.
    remainder, dates = fact_forms.extract_dates(fact_forms.prepare(stripped))
    return _facts(remainder) | dates


def _support_facts(text: str) -> set:
    """Facts the cited passages support, in every form they are written.

    The tokens as printed, plus each unambiguous date as a whole, the numerals
    behind number words ("seven" supports 7) and the two years of a range
    ("2024-25" supports 2024 and 2025). Only this side is widened: an
    answer's own wording is never turned into a figure it did not state.
    """
    prepared = fact_forms.prepare(text)
    supported = _facts(prepared)
    dates = fact_forms.extract_dates(prepared)[1]
    # A full date also supports an answer that leaves its year out.
    supported |= dates | fact_forms.year_less(dates)
    supported |= fact_forms.number_words(prepared)
    for fact in list(supported):
        supported |= fact_forms.range_years(fact)
    return supported


# A year, or an academic year, as a title states one.
YEAR_LIKE = re.compile(r"(?:19|20)\d{2}(?:-(?:19|20)\d{2})?")


def _title_facts(text: str) -> set:
    """The years a document's title states, as whole figures."""
    years = {
        fact for fact in _facts(fact_forms.prepare(text)) if YEAR_LIKE.fullmatch(fact)
    }
    for fact in list(years):
        years |= fact_forms.range_years(fact)
    return years


def _fact_supported(fact: str, supported: set, cores: set) -> bool:
    """Whether a figure the answer states is one the passages state.

    The same figure, or the same bare number with a unit or currency stuck to
    it. A RANGE ("2019-2021", "Rs 1500-2000") is supported by itself or by both
    of its ends: a passage that says "from 2019 to 2021" states it, and an
    answer that writes it with a dash must not lose to the dash.
    """
    if fact in supported or _numeric_core(fact) in cores:
        return True
    ends = fact_forms.range_parts(fact)
    return bool(ends) and all(_fact_supported(end, supported, cores) for end in ends)


def validate(answer_text: str, context, *, abstained: bool = False) -> GroundingReport:
    """Check an answer against the context it was supposed to use."""
    report = GroundingReport()
    # "[1, 2]" is two citations, written as a group.
    answer_text = fact_forms.normalize_markers(answer_text)
    report.text = answer_text

    if abstained or is_abstention(answer_text):
        # An abstention makes no claims, so there is nothing to ground. It
        # is the correct output when evidence is missing, not a failure.
        report.grounded = True
        return report

    available = set(context.by_marker())
    # A bracketed number that is not one of the context's markers but IS
    # printed in a retrieved passage ("Government Order [12]") is the
    # document's own reference, quoted - not a citation of passage 12.
    # Counting it made a verbatim, correct quote fail as ungrounded.
    #
    # Read the way a citation is read: a document's own "[2, 12]" is two
    # bracketed numbers, exactly as the answer's would be.
    quoted = {
        int(match.group(1))
        for passage in context.passages
        for match in MARKER.finditer(fact_forms.normalize_markers(passage.text))
    }
    cited = tuple(
        marker
        for marker in extract_markers(answer_text)
        if marker in available or marker not in quoted
    )
    report.cited_markers = cited

    # A citation to a passage that does not exist is the cheapest hallucination
    # signal there is, and it stays fatal. It was once forgiven when it was
    # only off by one ([0], or the number after the last passage) - and that
    # let a whole uncited, unsourced sentence through with its marker quietly
    # deleted.
    invalid = tuple(marker for marker in cited if marker not in available)
    report.invalid_markers = invalid
    if invalid:
        report.grounded = False
        report.failures.append(
            f"answer cites passage(s) {list(invalid)} that were not in the "
            f"context (available: {sorted(available)})"
        )

    if not cited:
        report.grounded = False
        report.failures.append(
            "answer makes claims but cites no passage, so nothing in it can be checked"
        )
        return report

    # Everything below is measured only against the passages the answer
    # actually cited. Scoring against the whole context would let an answer
    # borrow credibility from evidence it never referenced.
    by_marker = context.by_marker()
    supporting = " ".join(
        by_marker[marker].support_text for marker in cited if marker in by_marker
    )
    titles = " ".join(
        by_marker[marker].title_text for marker in cited if marker in by_marker
    )

    # --- Check 3: every stated fact is in a cited passage ----------------
    supported_facts = _support_facts(supporting)
    # A total, count, average, highest or lowest value that the cited tables'
    # own rows imply is not written anywhere, and is not an invention: it is
    # recomputed here, from the cells, rather than taken on the model's word.
    derived = tables.derived_figures(
        [by_marker[marker] for marker in cited if marker in by_marker]
    )
    supported_facts |= derived
    stated_facts = _answer_facts(answer_text)
    supported_cores = {_numeric_core(fact) for fact in supported_facts} - {None}
    # The year a document's title states ("Fee Structure 2024-25") is on the
    # page the reader checks against, and is the year of every figure in it. It
    # counts as a whole figure and nothing else: not through the bare-number
    # equivalence above ("Hostel Rules v2" does not support "2 weeks"), and
    # not a title's other digits.
    supported_facts |= _title_facts(titles)

    unsupported_facts = sorted(
        fact
        for fact in stated_facts
        if not _fact_supported(fact, supported_facts, supported_cores)
    )
    report.unsupported_facts = tuple(unsupported_facts)

    if unsupported_facts:
        report.grounded = False
        report.failures.append(
            "answer states figure(s) that appear in none of the passages it "
            f"cites: {unsupported_facts[:10]}"
        )

    # --- Check 4: the prose is drawn from those passages ------------------
    # A derived figure is evidence here too: "The total is 15,000" would
    # otherwise score as a sentence about words the table never uses.
    support_terms = (
        set(_prose_terms(supporting))
        | set(_prose_terms(titles))
        | set(_prose_terms(" ".join(derived)))
    )
    answer_terms = _prose_terms(MARKER.sub(" ", answer_text))

    if not answer_terms:
        report.support_ratio = 1.0
        return report

    unsupported = [term for term in answer_terms if term not in support_terms]
    report.support_ratio = round(1.0 - (len(unsupported) / len(answer_terms)), 4)
    report.unsupported_terms = tuple(sorted(set(unsupported))[:20])

    floor = getattr(settings, "RAG_MIN_SUPPORT_RATIO", None)
    if floor is None:
        floor = MIN_SUPPORT_RATIO

    if report.support_ratio < floor:
        report.grounded = False
        report.failures.append(
            f"only {report.support_ratio:.0%} of the answer's content words "
            f"appear in the passages it cites (minimum {floor:.0%}), which "
            f"suggests it is answering from something other than the evidence"
        )

    return report


def citations_for(answer_text: str, context) -> list:
    """Resolve an answer's markers to full citations, in cited order."""
    by_marker = context.by_marker()
    citations = []

    for marker in extract_markers(fact_forms.normalize_markers(answer_text)):
        passage = by_marker.get(marker)
        if passage is None:
            continue
        citation = passage.citation
        citation["marker"] = marker
        citation["relevance"] = passage.relevance
        citations.append(citation)

    return citations


# At most this many figures are re-attributed in one answer. A repair that
# needed more than two is an answer that was not written from what it cites.
MAX_REPAIRS = 2

# A full stop that ends a sentence is followed by whitespace; the one inside
# "B.Tech", "3.5" or "15.03.2025" is not. A line break ends one too.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKER_TOKEN = re.compile(r"\[\d{1,3}\]")

# What a clause and the passage it is attributed to must share besides the
# figure: two words, so the clause is about what the passage is about and not
# merely a number that happens to be in it.
MIN_SHARED_WORDS = 2


def _distinctive(fact: str) -> bool:
    """A figure specific enough that finding it in one passage means something.

    "2" or "10" turn up in most passages; a date, or a figure of three or more
    digits, does not. Only these are ever attributed to a passage.
    """
    return fact.startswith("date:") or len(re.sub(r"\D", "", fact)) >= 3


def _stated_in(passage, fact: str) -> bool:
    """Whether the passage's OWN text states the figure: not its title (which
    counts for a year and nothing else) and not its column labels."""
    supported = _support_facts(passage.text)
    return fact in supported or (
        _numeric_core(fact) is not None
        and _numeric_core(fact) in ({_numeric_core(f) for f in supported} - {None})
    )


def _words(text: str) -> set:
    return {term for term in _prose_terms(text) if not any(c.isdigit() for c in term)}


def repair_citations(report: GroundingReport, context):
    """(the answer with a marker added where one was missing, [(figure, marker)]).

    An answer that cites [1] for one clause and states a figure in a LATER
    clause that is written in [2], and in no other passage, did not invent it:
    it forgot to cite [2], and was discarded whole for it. The marker is added
    to that clause. It is deliberately narrow, because what it excuses is a
    figure the answer's own citations do not support:

    - Only a figure stated AFTER the last marker of its sentence. A figure that
      a marker follows ("The B.Tech hostel fee is Rs 85,000 [1]") is the model's
      own attribution, and if [1] does not state it the model mixed passages up:
      that is the failure the check exists to catch, and it is not repaired.
    - Only from a passage's own text, in exactly one uncited passage, and only
      when the clause shares at least two words with it.
    - Never a table row, never for an answer that cites nothing valid, at most
      twice, and the result is checked again from scratch.
    """
    text = report.text
    cited = set(report.cited_markers)
    if not report.unsupported_facts or not cited or report.invalid_markers:
        return text, []

    spans = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))

    repairs = []
    insertions = {}
    for fact in report.unsupported_facts:
        if len(repairs) >= MAX_REPAIRS:
            break
        if not _distinctive(fact):
            continue
        owners = [
            passage
            for passage in context.passages
            if passage.marker not in cited and _stated_in(passage, fact)
        ]
        if len(owners) != 1:
            continue
        owner = owners[0]

        for begin, finish in spans:
            sentence = text[begin:finish]
            if sentence.count("|") >= 2:
                continue
            markers = list(_MARKER_TOKEN.finditer(sentence))
            clause_begin = begin + (markers[-1].end() if markers else 0)
            clause = text[clause_begin:finish]
            if fact not in _answer_facts(clause):
                continue
            if len(_words(clause) & _words(owner.text)) < MIN_SHARED_WORDS:
                continue
            position = clause_begin + len(clause.rstrip(".!?;:, \t\n"))
            insertions.setdefault(position, set()).add(owner.marker)
            repairs.append((fact, owner.marker))
            break

    for position in sorted(insertions, reverse=True):
        marks = "".join(f" [{marker}]" for marker in sorted(insertions[position]))
        text = text[:position] + marks + text[position:]
    return text, repairs


def correction_for(report: GroundingReport) -> str:
    """What to tell a model whose answer stated figures its passages lack."""
    shown = [
        fact[len("date:") :] if fact.startswith("date:") else fact
        for fact in report.unsupported_facts[:8]
    ]
    return (
        "\n\nYour previous answer was rejected by an automated check: it stated "
        f"{', '.join(shown)}, which the passages you cited do not contain. "
        "Answer again. Write every figure and date exactly as a passage writes "
        "it, cite the passage each one comes from, and if the passages do not "
        "give a figure, say so instead of stating one."
    )
