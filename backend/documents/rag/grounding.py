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
    return _terms(THOUSANDS.sub("", text))


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

    @property
    def ok(self) -> bool:
        return self.grounded and not self.failures


def extract_markers(text: str) -> tuple:
    """Markers the answer cites, in the order they first appear."""
    seen = []
    for match in MARKER.finditer(text):
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
    return token.rstrip("./-:")


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
    return _facts(stripped)


def validate(answer_text: str, context, *, abstained: bool = False) -> GroundingReport:
    """Check an answer against the context it was supposed to use."""
    report = GroundingReport()

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
    quoted = {
        int(match.group(1))
        for passage in context.passages
        for match in MARKER.finditer(passage.text)
    }
    cited = tuple(
        marker
        for marker in extract_markers(answer_text)
        if marker in available or marker not in quoted
    )
    report.cited_markers = cited

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

    # --- Check 3: every stated fact is in a cited passage ----------------
    supported_facts = _facts(supporting)
    stated_facts = _answer_facts(answer_text)
    supported_cores = {_numeric_core(fact) for fact in supported_facts} - {None}

    unsupported_facts = sorted(
        fact
        for fact in stated_facts
        if fact not in supported_facts and _numeric_core(fact) not in supported_cores
    )
    report.unsupported_facts = tuple(unsupported_facts)

    if unsupported_facts:
        report.grounded = False
        report.failures.append(
            "answer states figure(s) that appear in none of the passages it "
            f"cites: {unsupported_facts[:10]}"
        )

    # --- Check 4: the prose is drawn from those passages ------------------
    support_terms = set(_prose_terms(supporting))
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

    for marker in extract_markers(answer_text):
        passage = by_marker.get(marker)
        if passage is None:
            continue
        citation = passage.citation
        citation["marker"] = marker
        citation["relevance"] = passage.relevance
        citations.append(citation)

    return citations
