"""The RAG engine: retrieve, rerank, build context, answer, verify.

    question
      -> permission-filtered hybrid retrieval   (documents.search)
      -> rerank
      -> context construction
      -> LLM gateway
      -> grounding + citation validation
      -> answer + sources, or an abstention

Every step can decide to abstain, and each records why. That matters more
than it looks: "no documents matched", "the passages were not relevant
enough" and "the model produced something that could not be grounded" are
three different facts about the system, and collapsing them into one silence
would hide which part is failing.
"""

import functools
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

from documents.observability import telemetry
from documents.search import service as search_service

from . import context as context_builder
from . import grounding
from .llm import ABSTENTION_TEXT

# How many retrieved candidates are handed to the reranker. Larger than the
# context can hold, because reranking is the step that decides which of them
# deserve a slot.
DEFAULT_RETRIEVAL_DEPTH = 20

# A passage must beat this after reranking to enter the context. Below it,
# the passage is a lexical coincidence, and building an answer from
# coincidences is how a system produces confident nonsense.
#
# This number belongs to a SCORING SCHEME, not to the system. The lexical
# reranker emits a weighted sum of three ratios; a cross-encoder emits a
# calibrated relevance probability, and 0.15 does not mean the same thing
# in both. A reranker may therefore declare `default_relevance_floor`, and
# `resolve_relevance_floor` prefers, in order: an explicit per-call
# override, an explicitly configured RAG_RELEVANCE_FLOOR, the reranker's
# own default, then this. Swapping in a reranker whose scores are
# distributed differently should not silently change how much evidence
# reaches the answer.
DEFAULT_RELEVANCE_FLOOR = 0.15


def resolve_relevance_floor(reranker, override=None, *, degraded=False) -> float:
    """The floor that belongs to the scoring scheme that produced the scores.

    `degraded` is the case that made this a function of the RESULT and not
    only of the configured reranker. Both cross-encoders fall back to the
    lexical reranker when their model is unavailable, and the scores they
    then return are lexical scores. Holding those to the cross-encoder's
    floor (0.30 for the local model) instead of the lexical one (0.15)
    turned a model outage into a wave of abstentions on questions the
    lexical scores would have answered - a degraded mode worse than the
    baseline it degrades to.
    """
    if override is not None:
        return override

    configured = getattr(settings, "RAG_RELEVANCE_FLOOR", None)
    if configured is not None:
        return configured

    if degraded:
        return DEFAULT_RELEVANCE_FLOOR

    return getattr(reranker, "default_relevance_floor", DEFAULT_RELEVANCE_FLOOR)


def _was_degraded(reranked) -> bool:
    """Whether a cross-encoder handed back lexical scores instead of its own."""
    return any(item.signals.get("reranker_degraded") for item in reranked)


class Abstention:
    NO_QUERY = "empty question"
    NO_DOCUMENTS = "no authorized documents matched the question"
    NO_RELEVANT_PASSAGES = "retrieved passages were not relevant enough"
    EMPTY_CONTEXT = "no passage survived context construction"
    MODEL_ABSTAINED = (
        "the answerer found nothing in the passages that answered the question"
    )
    UNGROUNDED = "the answer could not be grounded in the cited passages"
    # Distinguishable from the rest on purpose. "I could not find that" and
    # "I could not find that, and there is a figure on the page you are
    # asking about which I cannot read" are different facts about the
    # system, and only the second one tells the reader where to look.
    MAYBE_IN_A_FIGURE = (
        "no passage answered the question, and the documents retrieved "
        "contain figures this system cannot read"
    )


@dataclass
class RagAnswer:
    question: str
    answer: str
    abstained: bool
    abstention_reason: str = ""
    citations: list = field(default_factory=list)
    retrieved: int = 0
    # Document ids in RERANKED order, deduplicated, first occurrence wins.
    # Retrieval metrics (Recall@K, MRR) need the ranking, not the count -
    # a retriever that puts the right document first is better than one
    # that puts it tenth, and a count cannot tell those apart. Taken after
    # reranking rather than after context construction, because this is
    # meant to measure retrieval, not what survived the context budget.
    retrieved_document_ids: list = field(default_factory=list)
    reranked: int = 0
    passages_used: int = 0
    support_ratio: float = 0.0
    grounding_failures: list = field(default_factory=list)
    suspected_injections: int = 0
    model_name: str = ""
    model_version: str = ""
    generative: bool = False
    semantic_retrieval: bool = False
    reranker_name: str = ""
    duration_ms: int = 0
    # Milliseconds per stage - retrieval, rerank, generation, grounding.
    # duration_ms says a question was slow; this says which part was.
    timings: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


@functools.cache
def _load(path: str):
    return import_string(path)()


def reset_caches() -> None:
    _load.cache_clear()


def get_reranker():
    path = getattr(settings, "RAG_RERANKER", "")
    if not path:
        from .reranking import LexicalReranker

        return LexicalReranker()
    return _load(path)


def get_answerer():
    path = getattr(settings, "RAG_ANSWERER", "")
    if not path:
        from .llm import ExtractiveAnswerer

        return ExtractiveAnswerer()
    return _load(path)


def ranked_document_ids(reranked) -> list:
    """Document ids in rank order, first occurrence wins.

    Chunks are the retrieval unit but relevance is judged per document, so
    several chunks from one document collapse to one entry. Without this a
    single document could fill the top ten and score as ten successful
    retrievals.
    """
    ordered = []
    for item in reranked:
        document_id = item.chunk.document_version.document_id
        if document_id not in ordered:
            ordered.append(document_id)
    return ordered


# At most this many documents are named in an abstention. A hint listing
# forty documents is not a hint.
MAX_HINTED_DOCUMENTS = 3


def _figure_hints(document_ids) -> list:
    """Notes naming unreadable figures in documents retrieval already found.

    Scoped to `document_ids`, which come from the reranked hits - so every
    document named here has already passed the permission filter inside
    search. This adds no new path to a document a caller cannot see, which
    is the only way a feature like this can be safe: an "unreadable figure"
    hint about a document you are not allowed to know exists is still a
    disclosure.

    Restricted to figures with no description. A described figure is
    readable, and its text would have been retrievable in the ordinary way.
    """
    if not document_ids:
        return []

    from django.db.models import F

    from documents.models import DocumentFigure

    rows = (
        DocumentFigure.objects.filter(
            document_version__document_id__in=document_ids,
            description="",
        )
        # Only the version currently published for that document. Old
        # extraction passes still have their figures, and pointing a reader
        # at a page of a superseded version is worse than saying nothing.
        .filter(document_version=F("document_version__document__current_version"))
        .values_list("document_version__document__title", "page_number")
        .distinct()
        .order_by("document_version__document__title", "page_number")
    )

    pages_by_title = {}
    for title, page_number in rows:
        pages_by_title.setdefault(title, []).append(page_number)

    hints = []
    for title in list(pages_by_title)[:MAX_HINTED_DOCUMENTS]:
        pages = pages_by_title[title]
        label = "page" if len(pages) == 1 else "pages"
        # Worded for exactly what happened, which is narrower than "this
        # system cannot read images". OCR did run over these pages and did
        # extract any text inside them; what was never interpreted is the
        # image as an image - the bars of a chart, the shape of a diagram.
        # Saying "cannot read" would overclaim on a plain scan of a text
        # page, where everything that mattered was in fact extracted.
        hints.append(
            f"{title!r} has {len(pages)} image(s) on {label} "
            f"{', '.join(str(page) for page in pages)}; only text found "
            f"inside them was extracted, not the images themselves"
        )

    return hints


def _abstain(question, reason, started, **extra) -> RagAnswer:
    notes = list(extra.pop("notes", []))

    # Checked for every abstention that had documents to abstain about, so
    # a new abstention path added later cannot quietly lose the hint.
    if reason not in (Abstention.NO_QUERY, Abstention.NO_DOCUMENTS):
        hints = _figure_hints(extra.get("retrieved_document_ids") or [])
        if hints:
            notes += hints
            reason = Abstention.MAYBE_IN_A_FIGURE

    answer = RagAnswer(
        question=question,
        answer=ABSTENTION_TEXT,
        abstained=True,
        abstention_reason=reason,
        duration_ms=int((time.monotonic() - started) * 1000),
        notes=notes,
        **extra,
    )
    # Reported here rather than at each call site so no abstention path can
    # be added later that silently emits nothing - an abstention nobody
    # counts is exactly the signal worth alerting on.
    telemetry.rag_answered(answer)
    telemetry.rag_stage_timings(answer.timings)
    return answer


def _elapsed_ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def ask(
    user,
    question: str,
    *,
    document_type: str = "",
    department: str = "",
    document_id: int | None = None,
    focus: str = "",
    retrieval_depth: int = None,
    relevance_floor: float = None,
) -> RagAnswer:
    started = time.monotonic()
    question = (question or "").strip()

    if not question:
        return _abstain(question, Abstention.NO_QUERY, started)

    depth = retrieval_depth or getattr(
        settings, "RAG_RETRIEVAL_DEPTH", DEFAULT_RETRIEVAL_DEPTH
    )
    timings = {}

    # --- Retrieve (permission filter runs inside, before similarity) ------
    stage_started = time.monotonic()
    retrieval = search_service.search(
        user,
        question,
        limit=depth,
        document_type=document_type,
        department=department,
        document_id=document_id,
    )
    timings["retrieval"] = _elapsed_ms(stage_started)

    if not retrieval.hits:
        return _abstain(
            question,
            Abstention.NO_DOCUMENTS,
            started,
            semantic_retrieval=retrieval.semantic,
            notes=list(retrieval.notes),
            timings=timings,
        )

    # --- Rerank -----------------------------------------------------------
    reranker = get_reranker()
    stage_started = time.monotonic()
    reranked = reranker.rerank(question, retrieval.hits)
    timings["rerank"] = _elapsed_ms(stage_started)

    # Notes that describe how THIS answer was produced, as opposed to what
    # retrieval found. Both a degraded reranker and a fallen-back answerer
    # still produce a normal-looking response, so unless they are named here
    # a reader has no way to know the answer came from the baseline.
    notes = list(retrieval.notes)

    degraded = _was_degraded(reranked)
    if degraded:
        telemetry.rerank_degraded(reranker.name)
        notes.append(
            f"reranker {reranker.name} was unavailable; passages were scored "
            f"lexically instead"
        )

    floor = resolve_relevance_floor(reranker, relevance_floor, degraded=degraded)
    relevant = [item for item in reranked if item.score >= floor]

    if not relevant:
        best = reranked[0].score if reranked else 0.0
        return _abstain(
            question,
            Abstention.NO_RELEVANT_PASSAGES,
            started,
            retrieved=len(retrieval.hits),
            retrieved_document_ids=ranked_document_ids(reranked),
            reranked=len(reranked),
            reranker_name=reranker.name,
            semantic_retrieval=retrieval.semantic,
            notes=[*notes, f"best relevance {best:.3f} below floor {floor:.3f}"],
            timings=timings,
        )

    # --- Build context ----------------------------------------------------
    built = context_builder.build(
        relevant,
        budget=getattr(
            settings, "RAG_CONTEXT_BUDGET", context_builder.DEFAULT_CONTEXT_BUDGET
        ),
        max_passages=getattr(settings, "RAG_MAX_PASSAGES", 8),
    )

    if built.is_empty:
        return _abstain(
            question,
            Abstention.EMPTY_CONTEXT,
            started,
            retrieved=len(retrieval.hits),
            retrieved_document_ids=ranked_document_ids(reranked),
            reranked=len(reranked),
            reranker_name=reranker.name,
            semantic_retrieval=retrieval.semantic,
            notes=notes,
            timings=timings,
        )

    built.focus = (focus or "").strip()
    prompt = context_builder.render(built, question)

    # --- Answer -----------------------------------------------------------
    answerer = get_answerer()
    stage_started = time.monotonic()
    response = answerer.answer(question=question, context=built, prompt=prompt)
    timings["generation"] = _elapsed_ms(stage_started)

    # Usage is recorded for EVERY response, not only returned answers. An
    # answer that fails grounding still consumed its tokens, and counting
    # cost only on success under-reports it by exactly the answers that
    # were most wasteful.
    telemetry.llm_called(response, timings["generation"] / 1000.0)
    telemetry.llm_usage(response)

    fell_back_from = (response.metadata or {}).get("fell_back_from")
    if fell_back_from:
        notes.append(
            f"generative answerer {fell_back_from} was unavailable "
            f"({response.metadata.get('fallback_reason') or 'unspecified'}); "
            f"answered by quoting passages instead"
        )

    common = {
        "retrieved": len(retrieval.hits),
        "retrieved_document_ids": ranked_document_ids(reranked),
        "reranked": len(reranked),
        "passages_used": len(built.passages),
        "suspected_injections": built.suspected_injections,
        "model_name": response.model_name,
        "model_version": response.model_version,
        "generative": response.generative,
        "semantic_retrieval": retrieval.semantic,
        "reranker_name": reranker.name,
        "timings": timings,
    }

    if response.abstained:
        return _abstain(
            question, Abstention.MODEL_ABSTAINED, started, notes=notes, **common
        )

    # --- Verify before returning -----------------------------------------
    stage_started = time.monotonic()
    report = grounding.validate(response.text, built, abstained=False)
    timings["grounding"] = _elapsed_ms(stage_started)

    if not report.ok:
        # The model said something; it could not be substantiated. Section
        # 12.4 says abstain rather than invent, and an answer that fails
        # grounding is indistinguishable from an invention here.
        return _abstain(
            question,
            Abstention.UNGROUNDED,
            started,
            support_ratio=report.support_ratio,
            grounding_failures=list(report.failures),
            notes=notes,
            **common,
        )

    answer = RagAnswer(
        question=question,
        answer=response.text,
        abstained=False,
        citations=grounding.citations_for(response.text, built),
        support_ratio=report.support_ratio,
        duration_ms=int((time.monotonic() - started) * 1000),
        notes=notes,
        **common,
    )
    telemetry.rag_answered(answer)
    telemetry.rag_stage_timings(timings)
    return answer
