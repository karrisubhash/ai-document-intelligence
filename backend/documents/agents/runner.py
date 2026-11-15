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

from documents.rag import judging
from documents.rag.engine import Abstention, ask, get_answerer
from documents.rag.llm import ABSTENTION_TEXT, ExtractiveAnswerer

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
    # Per-stage milliseconds. Summed across steps for a planned question,
    # because each step is a full retrieval and the total is what the user
    # waited for.
    timings: dict = field(default_factory=dict)
    # How the answer was actually produced (RagAnswer's fields of the same
    # names): true when ANY step's reranker degraded or answerer fell back,
    # because a composed answer with one quoted part is partly quoted.
    reranker_degraded: bool = False
    answerer_fell_back: bool = False
    # The provider facts (documents.rag.engine.llm_metadata): the first
    # step's that reached a model, with tokens summed over every step.
    llm: dict = field(default_factory=dict)


def _combined_llm(results) -> dict:
    """One `llm` record for several steps' answers."""
    records = [result.llm for result in results if result.llm]
    if not records:
        return {}
    # The first record that names a model says what wrote the answer, as
    # model_name does; a step that only quoted contributes its fallback.
    combined = dict(
        next((record for record in records if record.get("model")), records[0])
    )
    combined["prompt_tokens"] = sum(r.get("prompt_tokens") or 0 for r in records)
    combined["completion_tokens"] = sum(
        r.get("completion_tokens") or 0 for r in records
    )
    combined["may_train_on_input"] = any(r.get("may_train_on_input") for r in records)
    fallback = next((r for r in records if r.get("fallback_reason")), None)
    if fallback is not None:
        for key in ("fallback_reason", "fallback_kind", "fallback_retry_at"):
            combined[key] = fallback.get(key)
    return combined


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

    # A widened follow-up arrives as "<the user's words> <borrowed terms>",
    # with the user's words as `focus`. Planning runs on the user's words
    # only. Planned on the widened string, "...the library fine? minimum
    # attendance required" split at the "? " into an extra part that
    # re-asked the PREVIOUS question - a step, a retrieval and an answer (or
    # an "unanswered" note) for something nobody asked this turn.
    focus = (kwargs.get("focus") or "").strip()
    parts = planner.plan(focus or question, max_steps=max_steps)

    # Planning exists for an answerer that can only quote: it answers one
    # thing per retrieval, so a compound question is cut up. A model reads a
    # dozen passages and answers all of it at once - and cutting the question
    # up costs it: a part loses what it referred to ("when is it due?"), every
    # part spends one of a free tier's few daily calls, and when every part
    # abstains chat's general reply is never tried. So while a model can
    # write the answer, the question goes to it whole.
    whole, planned_parts = None, []
    if len(parts) > 1 and not getattr(settings, "RAG_PLAN_WHEN_GENERATIVE", False):
        if judging.model_will_answer(get_answerer()):
            planned_parts, parts = parts, []

    if len(parts) <= 1:
        # Not compound, or planning is switched off. This is the ordinary
        # path and must behave exactly as it did before this module
        # existed - one retrieval, one answer, same object shape.
        result = ask(user, question, **kwargs)
        if not (planned_parts and _model_failed(result)):
            return _single(result, question, started)

        # The whole question went to a model and the model did not answer: a
        # timeout, a rate limit on every link, an empty or cut-off reply. What
        # is left is a quote, and a quote answers one thing per retrieval - the
        # four-part question this planner exists for is refused whole ("0 of
        # 4", above). So the question is cut up after all, and its parts are
        # answered by quoting, with no further model call: the model has had
        # its turn, and every call it was given is a call of the day.
        whole, parts = result, planned_parts
        kwargs = {**kwargs, "answerer": ExtractiveAnswerer()}

    # Each part then retrieves with the borrowed terms behind it, exactly
    # as a single follow-up does, and is answered against ITS OWN words.
    # With the whole multi-part question as every step's focus, a sentence
    # had to cover a third of the terms of all the parts together, so a
    # correct answer to one part ("The hostel curfew is 10 pm") was refused.
    widening = (
        question[len(focus) :].strip() if focus and question.startswith(focus) else ""
    )

    steps = []
    results = []
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
    timings = {}
    # Everything that was asked, the whole question first when a model was
    # given it and failed: the answer's provenance (what fell back, what was
    # spent, who saw the documents) is the whole attempt's, not only the parts'.
    asked = []
    if whole is not None:
        asked.append(whole)
        notes += whole.notes
        notes.append(
            f"the question went to the model whole and it did not answer it; its "
            f"{len(parts)} parts were answered by quoting passages instead"
        )
        for stage, milliseconds in whole.timings.items():
            timings[stage] = timings.get(stage, 0) + milliseconds

    for part in parts:
        if focus:
            query = f"{part} {widening}".strip()
            result = ask(user, query, **{**kwargs, "focus": part})
        else:
            result = ask(user, part, **kwargs)
        if _model_failed(result) and not isinstance(
            kwargs.get("answerer"), ExtractiveAnswerer
        ):
            # The model has had its turn: each further part would be another
            # wait on the same outage (three of them took 150 s against a 50 s
            # budget each), so the rest are quoted, as when the question went
            # whole and the model failed.
            kwargs = {**kwargs, "answerer": ExtractiveAnswerer()}
        results.append(result)
        asked.append(result)
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
        for stage, milliseconds in result.timings.items():
            timings[stage] = timings.get(stage, 0) + milliseconds

        if result.abstained:
            unanswered.append(part)
            continue

        # Past the highest marker already composed, not the number of
        # citations so far: a step that cited only its [2] holds one
        # citation, and offsetting the next step by one turned that step's
        # [1] into a second [2] - one marker naming two passages.
        offset = max(
            (int(value) for text in composed for value in MARKER.findall(text)),
            default=0,
        )
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
            timings=timings,
            reranker_degraded=any(r.reranker_degraded for r in asked),
            answerer_fell_back=any(r.answerer_fell_back for r in asked),
            llm=_combined_llm(asked),
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
        timings=timings,
        reranker_degraded=any(r.reranker_degraded for r in asked),
        answerer_fell_back=any(r.answerer_fell_back for r in asked),
        llm=_combined_llm(asked),
    )


def _model_failed(result) -> bool:
    """Whether a model was given the question and did not answer it: the
    answerer fell back to quoting (an outage, a rate limit, no key), its
    reply was empty, cut off or declined (ANSWERER_FAILED), or its first draft
    was rejected and the corrective retry never reached a model (the engine
    records `retry_fallback_reason`). In that last case the first failure
    stands and the question is refused whole, because a quote of the WHOLE
    compound question covers one part at best; the parts can each be quoted."""
    return (
        bool(result.answerer_fell_back)
        or result.abstention_reason == Abstention.ANSWERER_FAILED
        or bool((getattr(result, "llm", None) or {}).get("retry_fallback_reason"))
    )


def _single(result, question: str, started: float) -> PlannedAnswer:
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
        timings=dict(result.timings),
        reranker_degraded=result.reranker_degraded,
        answerer_fell_back=result.answerer_fell_back,
        llm=dict(result.llm),
    )
