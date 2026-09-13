"""Grounding and citation validation.

An answer is not trusted because a model produced it. Before anything is
returned, three questions get asked of it:

1. Does every marker it cites exist in the context it was given? A citation
   to [7] when six passages were supplied is a fabricated reference, and it
   is the single cheapest hallucination to detect.
2. Did it cite anything at all? A substantive claim with no citation cannot
   be checked by the person reading it.
3. Is what it says actually supported by the passages? Cheap to approximate
   and impossible to do perfectly without another model, so what is measured
   is reported as what it is - a support ratio, not a proof.

A failure here downgrades the answer to an abstention. Section 12.4 is
explicit that abstaining beats inventing, and an answer that cannot be
grounded is exactly the case that rule exists for.
"""

import re
from dataclasses import dataclass, field

from .llm import ABSTENTION_TEXT
from .reranking import _terms

MARKER = re.compile(r"\[(\d{1,3})\]")

# Share of an answer's content words that must appear in the cited passages.
# Deliberately not 1.0: an extractive answer scores ~1.0, but a generative
# model legitimately uses connective words that appear nowhere in the source.
# Set where an answer that merely *mentions* the passages fails, while one
# built from them passes.
MIN_SUPPORT_RATIO = 0.55


@dataclass
class GroundingReport:
    grounded: bool = True
    cited_markers: tuple = ()
    invalid_markers: tuple = ()
    support_ratio: float = 0.0
    unsupported_terms: tuple = ()
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


def validate(answer_text: str, context, *, abstained: bool = False) -> GroundingReport:
    """Check an answer against the context it was supposed to use."""
    report = GroundingReport()

    if abstained or answer_text.strip() == ABSTENTION_TEXT:
        # An abstention makes no claims, so there is nothing to ground. It
        # is the correct output when evidence is missing, not a failure.
        report.grounded = True
        return report

    available = set(context.by_marker())
    cited = extract_markers(answer_text)
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
            "answer makes claims but cites no passage, so nothing in it can "
            "be checked"
        )
        return report

    # Support is measured only against the passages the answer actually
    # cited. Scoring against the whole context would let an answer borrow
    # credibility from evidence it never referenced.
    by_marker = context.by_marker()
    supporting = " ".join(
        by_marker[marker].support_text for marker in cited if marker in by_marker
    )
    support_terms = set(_terms(supporting))

    stripped = MARKER.sub(" ", answer_text)
    answer_terms = [term for term in _terms(stripped)]

    if not answer_terms:
        report.support_ratio = 1.0
        return report

    unsupported = [term for term in answer_terms if term not in support_terms]
    report.support_ratio = round(
        1.0 - (len(unsupported) / len(answer_terms)), 4
    )
    report.unsupported_terms = tuple(sorted(set(unsupported))[:20])

    if report.support_ratio < MIN_SUPPORT_RATIO:
        report.grounded = False
        report.failures.append(
            f"only {report.support_ratio:.0%} of the answer's content words "
            f"appear in the passages it cites (minimum {MIN_SUPPORT_RATIO:.0%})"
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
