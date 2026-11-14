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
import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

from documents.observability import telemetry, tracing
from documents.search import service as search_service

from . import context as context_builder
from . import grounding, judging, reranking, streaming, tables
from .llm import ABSTENTION_TEXT

logger = logging.getLogger("adi.rag")

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


def was_degraded(reranked) -> bool:
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
    # The model declined, was cut off at the token limit or returned
    # nothing - or was unavailable, and quoting passages found nothing
    # either. The passages may well have held the answer, so this must not
    # read as MODEL_ABSTAINED's "the documents do not say": chat's
    # general-knowledge reply took it that way and replaced a cited answer,
    # cut off mid-way, with "your documents do not cover this".
    ANSWERER_FAILED = "the answerer could not produce a usable answer"
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
    # How this answer was ACTUALLY produced, for anything that aggregates
    # answers (evaluation): the configured reranker or answerer was not the
    # one that ran.
    reranker_degraded: bool = False
    answerer_fell_back: bool = False
    # What the model's provider told us about this answer (llm_metadata):
    # which provider and model wrote it, what it fell back from, tokens,
    # finish reason, whether what was sent may be trained on. Empty when no
    # answerer ran, or the configured one never calls a model.
    llm: dict = field(default_factory=dict)


def llm_metadata(response) -> dict:
    """The provider facts on one answerer response, as stored on a message.

    Numbers, model ids and exception class names only - never the provider's
    error text, which can echo what was sent.
    """
    meta = response.metadata or {}
    fell_back = bool(meta.get("fell_back_from"))
    if not (response.generative or fell_back):
        return {}
    details = {
        "provider": getattr(response, "provider", "") or "",
        "model": getattr(response, "model", "") or "",
        "served_by": meta.get("served_by", ""),
        "fell_back_from": getattr(response, "fell_back_from", "") or "",
        "finish_reason": getattr(response, "finish_reason", "") or "",
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "may_train_on_input": bool(getattr(response, "may_train_on_input", False)),
        "purpose": meta.get("purpose", ""),
    }
    if "cost" in meta:
        details["cost"] = meta["cost"]
    if fell_back:
        # The passages were quoted because no model answered: why, and
        # whether it comes back by itself ("capacity") or needs someone.
        details["fallback_reason"] = meta.get("fallback_reason", "")
        details["fallback_kind"] = meta.get("fallback_kind", "error")
        details["fallback_retry_at"] = meta.get("fallback_retry_at")
        details["attempts"] = list(meta.get("attempts") or [])
    return details


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

# A page with at least this much extracted text has been read, whatever images
# it also carries; see _figure_hints. About three lines of prose: a chart
# with a title and axis labels stays under it, a scanned page that OCR read
# does not.
FIGURE_PAGE_TEXT_CHARACTERS = 300


def _figure_hints(document_ids) -> list:
    """Notes naming unreadable figures in documents retrieval already found.

    Scoped to `document_ids`, which come from the reranked hits - so every
    document named here has already passed the permission filter inside
    search. This adds no new path to a document a caller cannot see, which
    is the only way a feature like this can be safe: an "unreadable figure"
    hint about a document you are not allowed to know exists is still a
    disclosure.

    Callers pass only documents with a passage above the relevance floor.
    Semantic retrieval returns its full depth for almost any question, so
    "every reranked document" included ones the reranker had just judged
    unrelated - and on a corpus with a logo in any of them, nearly every
    unanswerable question became MAYBE_IN_A_FIGURE, which chat's
    general-knowledge reply is not allowed to replace.

    Restricted to figures with no description. A described figure is
    readable, and its text would have been retrievable in the ordinary way.
    """
    if not document_ids:
        return []

    from django.db.models import F, Sum

    from documents.models import DocumentChunk, DocumentFigure

    rows = (
        DocumentFigure.objects.filter(
            document_version__document_id__in=document_ids,
            description="",
        )
        # Only the version currently published for that document. Old
        # extraction passes still have their figures, and pointing a reader
        # at a page of a superseded version is worse than saying nothing.
        .filter(document_version=F("document_version__document__current_version"))
        # ...and only the extraction pass currently live for it, as the text
        # query below is: a reprocess keeps the old pass's figures, and hinting
        # at one of them while judging "was this page read" by the new pass
        # compared two different extractions of the same page.
        .filter(document_content=F("document_version__current_content"))
        .values_list(
            "document_version__document_id",
            "document_version__document__title",
            "page_number",
        )
        .distinct()
        .order_by("page_number")
    )

    # Per document, not per title: two documents can share a title, and
    # their pages were listed as one document's.
    titles = {}
    pages_by_document = {}
    for document_id, title, page_number in rows:
        titles[document_id] = title
        pages_by_document.setdefault(document_id, []).append(page_number)

    # A page whose text was read is not a page that cannot be read. Every
    # embedded image is a "figure", and a scanned page is one image the size
    # of the page: OCR read it into chunks, yet it was reported as an
    # unreadable figure, which relabelled every unanswered question over a
    # scanned document MAYBE_IN_A_FIGURE - the abstention chat's general
    # reply may not replace - and told the reader to look at a page whose
    # text we hold.
    if pages_by_document:
        text_pages = {
            (row["document_version__document_id"], row["page_number"])
            for row in DocumentChunk.objects.filter(
                document_version__document_id__in=list(pages_by_document),
                document_version=F("document_version__document__current_version"),
                document_content=F("document_version__current_content"),
            )
            .order_by()
            .values("document_version__document_id", "page_number")
            .annotate(characters=Sum("character_count"))
            if row["characters"] >= FIGURE_PAGE_TEXT_CHARACTERS
        }
        for document_id in list(pages_by_document):
            pages = [
                page
                for page in pages_by_document[document_id]
                if (document_id, page) not in text_pages
            ]
            if pages:
                pages_by_document[document_id] = pages
            else:
                del pages_by_document[document_id]

    # In retrieval order, so the cap drops the least likely documents. In
    # title order it could drop the best-ranked one for three that sort
    # earlier alphabetically.
    rank = {}
    for position, document_id in enumerate(document_ids):
        rank.setdefault(document_id, position)
    hinted = sorted(pages_by_document, key=rank.__getitem__)[:MAX_HINTED_DOCUMENTS]

    hints = []
    for document_id in hinted:
        title = titles[document_id]
        pages = pages_by_document[document_id]
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


def _check(text, built):
    """grounding.validate, failing CLOSED.

    The check is the one thing standing between a model's words and a reader,
    and it parses whatever the documents and the model happen to contain. If it
    ever raises - a spelling nobody anticipated, a malformed passage - the
    answer is WITHHELD, as it would be for any other failure, and the failure
    is logged. It must not become a 500, and it must never become a pass.
    """
    try:
        return grounding.validate(text, built, abstained=False)
    except Exception:  # noqa: BLE001 - see above
        logger.exception("grounding.check_failed")
        report = grounding.GroundingReport(grounded=False, text=text or "")
        report.failures.append(
            "the grounding check itself failed on this answer, so it was withheld"
        )
        return report


def _repair(report, built):
    """grounding.repair_citations, which may only ever help: an error leaves
    the answer as it was."""
    try:
        return grounding.repair_citations(report, built)
    except Exception:  # noqa: BLE001
        logger.exception("grounding.repair_failed")
        return report.text, []


def _abstain(question, reason, started, **extra) -> RagAnswer:
    notes = list(extra.pop("notes", []))
    # Documents with a passage above the relevance floor - the only ones
    # whose figures are evidence of where the answer is. Not stored on the
    # answer: retrieved_document_ids already records the ranking.
    relevant_document_ids = extra.pop("relevant_document_ids", None) or []

    # Checked for every abstention that had documents to abstain about, so
    # a new abstention path added later cannot quietly lose the hint.
    if reason not in (Abstention.NO_QUERY, Abstention.NO_DOCUMENTS):
        hints = _figure_hints(relevant_document_ids)
        if hints:
            notes += hints
            # UNGROUNDED means an answer WAS produced and failed verification
            # - the hallucination signal telemetry counts by reason. A logo
            # or letterhead in a retrieved document is not a reason to
            # relabel that; the hint stays as a note. Nor is it a reason to
            # relabel a model that failed to answer: its passages were found.
            if reason not in (Abstention.UNGROUNDED, Abstention.ANSWERER_FAILED):
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


def ask(user, question: str, **options) -> RagAnswer:
    """Answer `question` for `user`; see _ask. Traced as one `rag.ask` span."""
    with tracing.span("rag.ask") as current:
        answer = _ask(user, question, **options)
        tracing.record_rag_answer(current, answer)
        return answer


def _ask(
    user,
    question: str,
    *,
    document_type: str = "",
    department: str = "",
    document_id: int | None = None,
    collection_id: int | None = None,
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
    # Progress for a streaming reader (documents.rag.streaming); a no-op
    # anywhere else.
    streaming.status(streaming.RETRIEVING)
    stage_started = time.monotonic()
    retrieval = search_service.search(
        user,
        question,
        limit=depth,
        document_type=document_type,
        department=department,
        document_id=document_id,
        collection_id=collection_id,
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
    streaming.status(streaming.RANKING)
    reranker = get_reranker()
    stage_started = time.monotonic()
    # A score remembered while reranking serves only this user's later
    # questions; see reranking.cache_scope.
    with reranking.cache_scope(getattr(user, "pk", None)):
        reranked = reranker.rerank(question, retrieval.hits)
    timings["rerank"] = _elapsed_ms(stage_started)

    # Notes that describe how THIS answer was produced, as opposed to what
    # retrieval found. Both a degraded reranker and a fallen-back answerer
    # still produce a normal-looking response, so unless they are named here
    # a reader has no way to know the answer came from the baseline.
    notes = list(retrieval.notes)

    degraded = was_degraded(reranked)
    if degraded:
        telemetry.rerank_degraded(reranker.name)
        notes.append(
            f"reranker {reranker.name} was unavailable; passages were scored "
            f"lexically instead"
        )

    floor = resolve_relevance_floor(reranker, relevance_floor, degraded=degraded)
    relevant = [item for item in reranked if item.score >= floor]

    # A reranker judges whether one passage answers a question, which a table
    # rarely does for a question that compares, counts or adds: "who has the
    # highest earned leave?" scored a staff table 0.14 against a floor of 0.30
    # and was answered "not found" with the table in front of us. Tables that
    # retrieval ranked highly and that share terms with the question go on to
    # the answerer, whose own refusal and the grounding check still apply.
    retrieval_ranks = {
        hit.chunk.id: rank for rank, hit in enumerate(retrieval.hits, start=1)
    }
    admitted = tables.admit_below_floor(question, reranked, floor, retrieval_ranks)
    if admitted:
        admitted_ids = {id(item) for item in admitted}
        relevant = [
            item for item in reranked if item.score >= floor or id(item) in admitted_ids
        ]
        notes.append(
            f"{len(admitted)} table passage(s) scored below the relevance "
            f"floor (best {admitted[0].score:.3f} < {floor:.3f}) and were "
            f"passed to the answerer anyway: retrieval ranked them in its top "
            f"{tables.ADMIT_WITHIN_RETRIEVAL_RANK} and the question shares "
            f"terms with their columns or cells"
        )

    # What the reranker itself vouched for. Figure hints and the previewed
    # sources describe THIS set only; passages a model is merely asked to
    # judge (below) are not evidence of where an answer is.
    evidence = relevant

    # A generative model, not the score floor, decides whether a passage
    # answers the question. See documents.rag.judging.
    answerer = get_answerer()
    # Whether a model will write this answer. The larger context, the chunks
    # beside the best passages and the corrective retry follow from that;
    # handing it the passages the floor rejects is a separate switch.
    model_writes = judging.model_will_answer(answerer)
    if model_writes:
        judged = []
        leading = []
        if judging.enabled():
            judged = judging.admit(reranked, evidence, retrieval_ranks)
            # "Already in the context" means in the context: a passage that was
            # retrieved but not admitted is not, and is exactly the chunk that
            # continues a list.
            in_context = {item.chunk.id for item in [*evidence, *judged]}
            leading = judging.leading_chunks(user, document_id, in_context)
        judged_ids = {id(item) for item in judged}
        evidence_ids = {id(item) for item in evidence}
        if judged or leading:
            relevant = [
                item
                for item in reranked
                if id(item) in evidence_ids or id(item) in judged_ids
            ] + leading
            notes.append(
                f"{len(judged) + len(leading)} passage(s) below the relevance "
                f"floor ({floor:.3f}) were passed to the generative model to "
                f"judge: the reranker ranked them in its top "
                f"{getattr(settings, 'RAG_MIN_PASSAGES', judging.DEFAULT_MIN_PASSAGES)}"
                f" or retrieval in its top "
                f"{getattr(settings, 'RAG_ADMIT_RETRIEVAL_TOP', judging.DEFAULT_RETRIEVAL_TOP)}"
                f"{', or they open the document asked about' if leading else ''}"
            )

        # The chunks either side of the best passages, so an answer that runs
        # over a chunk boundary reaches the model whole. Only around what the
        # reranker vouched for, and after every other passage, so they fill
        # whatever room is left and displace nothing.
        beside = judging.neighbours(
            user, evidence, {item.chunk.id for item in relevant}
        )
        if beside:
            relevant = [*relevant, *beside]
            notes.append(
                f"{len(beside)} neighbouring passage(s) were added beside the "
                f"best-ranked ones"
            )

    if not relevant:
        # No relevant_document_ids: nothing passed the floor, so no figure
        # here is evidence of anything.
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
            reranker_degraded=degraded,
        )

    # --- Build context ----------------------------------------------------
    # A model is shown more than a quote can use: see RAG_CONTEXT_BUDGET_GENERATIVE.
    # The quoting path keeps its own, so what was measured for it does not move.
    budget, max_passages = (
        (
            getattr(settings, "RAG_CONTEXT_BUDGET_GENERATIVE", 14000),
            getattr(settings, "RAG_MAX_PASSAGES_GENERATIVE", 12),
        )
        if model_writes
        else (
            getattr(
                settings, "RAG_CONTEXT_BUDGET", context_builder.DEFAULT_CONTEXT_BUDGET
            ),
            getattr(settings, "RAG_MAX_PASSAGES", 8),
        )
    )
    built = context_builder.build(
        relevant,
        budget=budget,
        max_passages=max_passages,
        # A degraded cross-encoder scored lexically, so it gets the lexical
        # rules too.
        semantic_scores=bool(getattr(reranker, "semantic_scores", False))
        and not degraded,
    )

    if built.is_empty:
        # The counters are the whole explanation for this abstention; without
        # them a stored message cannot say why relevant passages vanished.
        notes = [
            *notes,
            f"context construction dropped all {len(relevant)} passage(s) above "
            f"the floor: {built.dropped_as_tiny} too short, "
            f"{built.dropped_as_duplicate} duplicate, "
            f"{built.dropped_for_budget} over budget",
        ]
        return _abstain(
            question,
            Abstention.EMPTY_CONTEXT,
            started,
            retrieved=len(retrieval.hits),
            retrieved_document_ids=ranked_document_ids(reranked),
            relevant_document_ids=ranked_document_ids(evidence),
            reranked=len(reranked),
            reranker_name=reranker.name,
            semantic_retrieval=retrieval.semantic,
            notes=notes,
            timings=timings,
            reranker_degraded=degraded,
        )

    built.focus = (focus or "").strip()
    prompt = context_builder.render(built, question)
    # The passages the answer is about to be written from - authorized, above
    # the floor and inside the budget - so a reader sees where it is looking
    # before the first word. Titles and pages only.
    # Not the passages a model was only asked to judge: a reader would be
    # shown titles of documents the answer may turn out to have nothing to do
    # with.
    previewed = [passage for passage in built.passages if not passage.model_judged]
    if previewed:
        streaming.sources(previewed)
    streaming.status(streaming.WRITING)

    # --- Answer -----------------------------------------------------------
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
        "reranker_degraded": degraded,
        "answerer_fell_back": bool(fell_back_from),
        "timings": timings,
        "llm": llm_metadata(response),
    }

    if response.abstained:
        # A model that refused, or whose reply was cut off at the token
        # limit or came back empty, is a different fact from one that found
        # nothing in the passages - and MODEL_ABSTAINED says only the last.
        reason = (response.metadata or {}).get("reason")
        failed = bool(response.generative and reason) or bool(fell_back_from)
        if response.generative and reason:
            notes.append(f"the model did not answer: {reason}")
        # A streaming client discards what it was shown (a cut-off draft, or
        # the model's own refusal) for the reply that follows.
        streaming.reset()
        return _abstain(
            question,
            Abstention.ANSWERER_FAILED if failed else Abstention.MODEL_ABSTAINED,
            started,
            notes=notes,
            relevant_document_ids=ranked_document_ids(evidence),
            **common,
        )

    # --- Verify before returning -----------------------------------------
    streaming.status(streaming.VERIFYING)
    stage_started = time.monotonic()
    report = _check(response.text, built)
    timings["grounding"] = _elapsed_ms(stage_started)

    # A rejected answer that states figures the passages lack gets two chances
    # before it is thrown away, each of which can only make it TRACEABLE:
    #
    # 1. It cited [1] and its figure is written in [2], and in no other
    #    passage: the marker is added, and the answer checked again from
    #    scratch (grounding.repair_citations).
    # 2. Otherwise, a model that can answer now is told which figures were
    #    not in its passages and asked once more. What it writes is validated
    #    exactly as the first answer was; if it fails again the FIRST failure
    #    stands, so the hallucination signal still counts it.
    if not report.ok and report.unsupported_facts and not report.invalid_markers:
        repaired_text, repairs = _repair(report, built)
        if repairs:
            repaired = _check(repaired_text, built)
            if repaired.ok:
                report = repaired
                telemetry.grounding_recovery("repaired")
                notes.append(
                    "added the citation the answer left out for "
                    + ", ".join(f"{fact} -> [{marker}]" for fact, marker in repairs)
                )

        if (
            not report.ok
            and getattr(settings, "RAG_GROUNDING_RETRY", True)
            and response.generative is True
            and not fell_back_from
            and judging.model_will_answer(answerer)
        ):
            # A client was shown the first draft as it streamed.
            streaming.reset()
            streaming.status(streaming.WRITING)
            stage_started = time.monotonic()
            second = answerer.answer(
                question=question,
                context=built,
                prompt=prompt + grounding.correction_for(report),
            )
            timings["retry"] = _elapsed_ms(stage_started)
            telemetry.llm_called(second, timings["retry"] / 1000.0)
            telemetry.llm_usage(second)

            second_fell_back = (second.metadata or {}).get("fell_back_from")
            second_report = (
                _check(second.text, built)
                if second.generative is True
                and not second.abstained
                and not second_fell_back
                else None
            )
            retried = "; ".join(report.failures[:1])

            # What this answer cost is both calls, whichever one is shown.
            first_llm = dict(common.get("llm") or {})
            second_llm = llm_metadata(second)
            spent = {
                key: (first_llm.get(key) or 0) + (second_llm.get(key) or 0)
                for key in ("prompt_tokens", "completion_tokens")
            }

            if second_report is not None and second_report.ok:
                notes.append(
                    f"the first answer was rejected ({retried}); "
                    f"asked the model once more and its second answer was grounded"
                )
                response, report = second, second_report
                telemetry.grounding_recovery("retried_grounded")
                common.update(
                    model_name=response.model_name,
                    model_version=response.model_version,
                    generative=response.generative,
                    llm={**second_llm, **spent} if second_llm else first_llm,
                )
            else:
                notes.append(
                    f"the first answer was rejected ({retried}); asked the model "
                    f"once more and it did not produce a grounded answer either"
                )
                telemetry.grounding_recovery("retried_failed")
                if first_llm:
                    common["llm"] = {**first_llm, **spent}

    if not report.ok:
        # The model said something; it could not be substantiated. Section
        # 12.4 says abstain rather than invent, and an answer that fails
        # grounding is indistinguishable from an invention here.
        # Withdrawn from a streaming client too, which has been showing it,
        # citation markers and all, as the answer.
        streaming.reset()
        return _abstain(
            question,
            Abstention.UNGROUNDED,
            started,
            support_ratio=report.support_ratio,
            grounding_failures=list(report.failures),
            notes=notes,
            relevant_document_ids=ranked_document_ids(evidence),
            **common,
        )

    answer = RagAnswer(
        question=question,
        # What grounding checked and cleaned: a group of citations split into
        # one marker each, a stray off-by-one marker removed.
        answer=report.text or response.text,
        abstained=False,
        citations=grounding.citations_for(report.text or response.text, built),
        support_ratio=report.support_ratio,
        duration_ms=int((time.monotonic() - started) * 1000),
        notes=notes,
        **common,
    )
    telemetry.rag_answered(answer)
    telemetry.rag_stage_timings(timings)
    return answer
