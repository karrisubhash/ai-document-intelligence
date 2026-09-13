"""Running a plan: one grounded retrieval per part, composed honestly.

There is no synthesis step, and that is the point. Each part is answered by
the ordinary RAG engine - same permission filter, same reranking, same
grounding validation, same abstention rules - and the composed answer is
those answers, in order, with their own citations. Nothing here writes a
sentence that the engine did not already ground.

Two properties this has to preserve, both easy to lose:

*Markers stay resolvable.* Every step numbers its passages from [1], so
step two's [1] and step one's [1] are different passages. They are
renumbered into one sequence as they are composed, and the citations are
renumbered with them. A marker that resolves to the wrong passage is worse
than no citation, because it looks checkable and is not.

*An unanswered part is reported, not dropped.* A three-part question
answered in two parts must say which part went unanswered. Silently
returning two answers to a three-part question reads as a complete answer.
"""

import re
import time
from dataclasses import dataclass, field

from django.conf import settings

from documents.rag.engine import ask
from documents.rag.llm import ABSTENTION_TEXT

from . import planner

MARKER = re.compile(r"\[(\d{1,3})\]")


@dataclass
class PlanStep:
    question: str
    answer: str
    abstained: bool
    abstention_reason: str = ""
    citations: list = field(default_factory=list)
    support_ratio: float = 0.0
    notes: list = field(default_factory=list)


@dataclass
class PlannedAnswer:
    question: str
    answer: str
    abstained: bool
    abstention_reason: str = ""
    citations: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    unanswered: list = field(default_factory=list)
    planned: bool = False
    duration_ms: int = 0
    model_name: str = ""
    model_version: str = ""
    generative: bool = False
    support_ratio: float = 0.0
    notes: list = field(default_factory=list)
    # Aggregated across the steps, so a planned answer reports the same
    # facts a single one does. Summed rather than averaged: a three-step
    # answer really did retrieve three times, and reporting the mean would
    # understate what the question cost.
    retrieved: int = 0
    reranked: int = 0
    passages_used: int = 0
    suspected_injections: int = 0
    semantic_retrieval: bool = False
    reranker_name: str = ""
    grounding_failures: list = field(default_factory=list)


def _renumber(text: str, offset: int) -> str:
    """Shift every marker in `text` by `offset`, in one pass.

    One pass matters: rewriting [1] to [3] and then [2] to [4] over the
    same string would re-match the [3] this rewrite just produced.
    """
    return MARKER.sub(lambda match: f"[{int(match.group(1)) + offset}]", text)


def answer(user, question: str, **kwargs) -> PlannedAnswer:
    """Answer a question, planning it into parts only when that helps."""
    started = time.monotonic()
    question = (question or "").strip()

    max_steps = getattr(settings, "RAG_MAX_PLAN_STEPS", planner.MAX_STEPS)
    parts = planner.plan(question, max_steps=max_steps)

    if len(parts) <= 1:
        # Not compound, or planning is switched off. This is the ordinary
        # path and must behave exactly as it did before this module
        # existed - one retrieval, one answer, same object shape.
        return _single(user, question, started, **kwargs)

    steps = []
    composed = []
    citations = []
    unanswered = []
    notes = []
    answerer = None
    totals = {
        "retrieved": 0,
        "reranked": 0,
        "passages_used": 0,
        "suspected_injections": 0,
        "semantic": False,
        "reranker": "",
        "grounding_failures": [],
    }

    for part in parts:
        result = ask(user, part, **kwargs)
        if answerer is None and result.model_name:
            # Which answerer produced these answers, taken from the first
            # step that reached one. Reported rather than left blank so a
            # planned answer says what answered it, exactly as a single
            # one does.
            answerer = result

        step = PlanStep(
            question=part,
            answer=result.answer,
            abstained=result.abstained,
            abstention_reason=result.abstention_reason,
            citations=list(result.citations),
            support_ratio=result.support_ratio,
            notes=list(result.notes),
        )
        steps.append(step)
        notes += result.notes
        totals["retrieved"] += result.retrieved
        totals["reranked"] += result.reranked
        totals["passages_used"] += result.passages_used
        totals["suspected_injections"] += result.suspected_injections
        totals["semantic"] = totals["semantic"] or result.semantic_retrieval
        totals["reranker"] = totals["reranker"] or result.reranker_name
        totals["grounding_failures"] += list(result.grounding_failures)

        if result.abstained:
            unanswered.append(part)
            continue

        offset = len(citations)
        composed.append(f"{part} {_renumber(result.answer, offset)}")
        for citation in result.citations:
            shifted = dict(citation)
            shifted["marker"] = citation["marker"] + offset
            citations.append(shifted)

    if not composed:
        return PlannedAnswer(
            question=question,
            answer=ABSTENTION_TEXT,
            abstained=True,
            abstention_reason=(
                f"none of the {len(parts)} parts of the question could be "
                f"answered from the authorized documents"
            ),
            steps=steps,
            unanswered=unanswered,
            planned=True,
            duration_ms=int((time.monotonic() - started) * 1000),
            notes=notes,
            retrieved=totals["retrieved"],
            reranked=totals["reranked"],
            passages_used=totals["passages_used"],
            suspected_injections=totals["suspected_injections"],
            semantic_retrieval=totals["semantic"],
            reranker_name=totals["reranker"],
            grounding_failures=totals["grounding_failures"],
        )

    if unanswered:
        # Said plainly, in the answer itself. A reader who sees two
        # paragraphs in reply to a three-part question has no way to know
        # the third part was not merely uninteresting.
        notes.append(f"{len(unanswered)} of {len(parts)} parts were not answered")

    answered = [step for step in steps if not step.abstained]
    ratios = [step.support_ratio for step in answered if step.support_ratio]

    return PlannedAnswer(
        question=question,
        answer="\n\n".join(composed),
        abstained=False,
        citations=citations,
        steps=steps,
        unanswered=unanswered,
        planned=True,
        duration_ms=int((time.monotonic() - started) * 1000),
        model_name=answerer.model_name if answerer else "",
        model_version=answerer.model_version if answerer else "",
        generative=answerer.generative if answerer else False,
        # The mean over the parts that were actually answered. Including
        # abstained parts as zero would report a two-of-three answer as
        # badly grounded when both answers it gave are fully grounded.
        support_ratio=round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        notes=notes,
        retrieved=totals["retrieved"],
        reranked=totals["reranked"],
        passages_used=totals["passages_used"],
        suspected_injections=totals["suspected_injections"],
        semantic_retrieval=totals["semantic"],
        reranker_name=totals["reranker"],
        grounding_failures=totals["grounding_failures"],
    )


def _single(user, question: str, started: float, **kwargs) -> PlannedAnswer:
    result = ask(user, question, **kwargs)

    return PlannedAnswer(
        question=result.question,
        answer=result.answer,
        abstained=result.abstained,
        abstention_reason=result.abstention_reason,
        citations=list(result.citations),
        steps=[
            PlanStep(
                question=question,
                answer=result.answer,
                abstained=result.abstained,
                abstention_reason=result.abstention_reason,
                citations=list(result.citations),
                support_ratio=result.support_ratio,
                notes=list(result.notes),
            )
        ],
        unanswered=[question] if result.abstained else [],
        planned=False,
        duration_ms=int((time.monotonic() - started) * 1000),
        model_name=result.model_name,
        model_version=result.model_version,
        generative=result.generative,
        support_ratio=result.support_ratio,
        notes=list(result.notes),
        retrieved=result.retrieved,
        reranked=result.reranked,
        passages_used=result.passages_used,
        suspected_injections=result.suspected_injections,
        semantic_retrieval=result.semantic_retrieval,
        reranker_name=result.reranker_name,
        grounding_failures=list(result.grounding_failures),
    )
