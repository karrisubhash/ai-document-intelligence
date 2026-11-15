"""Letting a generative model judge relevance, instead of a score floor.

The relevance floor exists because the extractive answerer QUOTES whatever it
is given: handed a passage that merely shares words with the question, it
returns it, confidently. A floor is the only protection that answerer has.

A generative model does not need it. It reads the passages and can say "these
do not answer that" - which is exactly the judgement the floor was standing in
for - and the floor is a worse judge than the model: the small cross-encoder
the chat path uses scores a correct, paraphrased passage 0.00-0.08 against a
floor of 0.30, while still ranking it first to third (docs/evaluation/
search-relevance.md). Gated by that score, the model never saw the passage
and the user was told "I couldn't find enough information" - or, with general
knowledge on, was given a general reply to a question about their own
documents.

So when a model is going to write the answer, the passages the floor rejects
but the reranker still ranks near the top - and the ones retrieval itself
ranked first - are handed over with the rest, flagged `model_judged`:

- They are evidence for a model that can decline, and for nothing else. The
  quoting fallback, the "unreadable figure" hints and the sources previewed
  to a reader before the answer all leave them out (see
  context.BuiltContext.without_model_judged).
- Only when a model can actually answer now (`model_can_judge`): with every
  provider out of quota the answer is a quote, and a quote needs the floor.
- Grounding still decides whether what the model wrote is supported, so a
  model that answers from an unrelated passage is caught by its figures and
  its citations, exactly as before.
"""

import re

from django.conf import settings
from django.db.models import Q

from . import gateway as gateway_module
from .context import DEFAULT_CONTEXT_BUDGET, BuiltContext
from .reranking import RerankedChunk

DEFAULT_MIN_PASSAGES = 6
DEFAULT_RETRIEVAL_TOP = 2
DEFAULT_NEIGHBOUR_ANCHORS = 3

# How many opening chunks of a document the question is scoped to are read
# even when none of them scored: "summarise this document" matches nothing in
# particular, and the answer is the start of it.
LEADING_CHUNKS = 3

# Shorter than this a passage is a heading or a page number. The cross-encoder
# used to vouch for short ones; these were never vouched for.
MIN_CHARS = 40

_HAS_LETTER = re.compile(r"[^\W\d_]")


def enabled() -> bool:
    return bool(getattr(settings, "RAG_MODEL_JUDGES_RELEVANCE", True))


def model_will_answer(answerer) -> bool:
    """Whether a generative model is expected to write this answer.

    Everything that is for a model's benefit follows from this alone: the
    larger context, the chunks beside the best passages, the corrective retry,
    answering a compound question whole. (Whether the model also judges
    relevance below the floor is a separate switch: `model_can_judge`.)

    `generative is True` rather than truthiness: a test double's attributes
    are all truthy. And a gateway that will not attempt a model at all (no
    key, or every link used up for the day) returns False, so the answer that
    is about to be a quote is not handed passages a quote cannot judge.

    The test is whether a model will be TRIED, not whether one is ready. Chat
    tries a link that is resting after a rate limit rather than give up
    (rag.gateway: "tried last"), and on a free tier with a handful of requests
    a minute per model most links are resting most of the time: asking for a
    ready one switched this whole path off exactly when the site was busiest.
    A resting link that fails again leaves a quote, which never sees the
    passages only a model was asked to judge. The check reads the budget and
    sends nothing.
    """
    if getattr(answerer, "generative", False) is not True:
        return False

    gateway = getattr(answerer, "gateway", None)
    if not isinstance(gateway, gateway_module.Gateway):
        # An answerer with no budget to consult (the Claude answerer). It can
        # still say it has no way to reach a model - the SDK is not installed,
        # Bedrock has no region - and then it is going to quote.
        can_answer = getattr(answerer, "can_answer", None)
        if callable(can_answer):
            try:
                return bool(can_answer())
            except Exception:  # noqa: BLE001 - as for the budget below
                return True
        return True

    try:
        return bool(gateway.plan(gateway_module.current_purpose()).links)
    except Exception:  # noqa: BLE001 - a budget outage must not decide answers
        # The gateway makes its own decision when the call is made, and the
        # quoting fallback never sees model_judged passages either way.
        return True


def model_can_judge(answerer) -> bool:
    """Whether the model is also handed the passages the relevance floor would
    reject (RAG_MODEL_JUDGES_RELEVANCE)."""
    return enabled() and model_will_answer(answerer)


def quote_context(context):
    """What a QUOTE may be written from, when a model was meant to answer.

    A model is shown a bigger context than a quote has ever been measured on
    (RAG_MAX_PASSAGES_GENERATIVE passages inside RAG_CONTEXT_BUDGET_GENERATIVE
    characters), flagged where it was only asked to judge a passage. When it
    then fails and the passages are quoted instead, the quote gets the passages
    the reranker vouched for (`without_model_judged`) - and no more of them than
    a quote always had, RAG_MAX_PASSAGES inside RAG_CONTEXT_BUDGET: more
    sentences tied for a place is a different answer from the one the quoting
    path was measured to give, and the settings say it does not move.

    The passages keep their markers, so what is quoted still resolves against
    the full context in grounding. Within the cap they are taken in order, and
    one that does not fit the budget is skipped for a smaller one after it, as
    `context.build` does.
    """
    vetted = context.without_model_judged()
    limit = getattr(settings, "RAG_MAX_PASSAGES", 8)
    budget = getattr(settings, "RAG_CONTEXT_BUDGET", DEFAULT_CONTEXT_BUDGET)

    kept, used = [], 0
    for passage in vetted.passages:
        if len(kept) >= limit:
            break
        if used + len(passage.text) > budget:
            continue
        kept.append(passage)
        used += len(passage.text)

    if len(kept) == len(vetted.passages):
        return vetted
    return BuiltContext(
        passages=kept,
        characters=used,
        dropped_for_budget=vetted.dropped_for_budget + len(vetted.passages) - len(kept),
        dropped_as_duplicate=vetted.dropped_as_duplicate,
        dropped_as_tiny=vetted.dropped_as_tiny,
        suspected_injections=sum(1 for p in kept if p.suspected_injection),
        focus=vetted.focus,
    )


def admit(reranked, kept, retrieval_ranks, *, minimum=None, retrieval_top=None) -> list:
    """Candidates below the floor that a model should still read.

    The union of: the reranker's own top `minimum` (counted over the whole
    ranked list, so passages already above the floor use up places), and the
    candidates retrieval ranked in its top `retrieval_top`. Returned in
    reranked order. Each one is flagged in its signals.
    """
    minimum = (
        getattr(settings, "RAG_MIN_PASSAGES", DEFAULT_MIN_PASSAGES)
        if minimum is None
        else minimum
    )
    retrieval_top = (
        getattr(settings, "RAG_ADMIT_RETRIEVAL_TOP", DEFAULT_RETRIEVAL_TOP)
        if retrieval_top is None
        else retrieval_top
    )

    kept_ids = {id(item) for item in kept}
    admitted = []
    for position, item in enumerate(reranked):
        if id(item) in kept_ids:
            continue
        rank = retrieval_ranks.get(item.chunk.id)
        near_the_top = position < minimum or (
            rank is not None and rank <= retrieval_top
        )
        if not near_the_top:
            continue
        text = (item.chunk.text or "").strip()
        if len(text) < MIN_CHARS or not _HAS_LETTER.search(text):
            continue
        item.signals["model_judged"] = True
        admitted.append(item)
    return admitted


def leading_chunks(user, document_id, taken_chunk_ids, *, limit=LEADING_CHUNKS) -> list:
    """The first chunks of one document, for a question scoped to it.

    Only what `user` may read (the same authorized queryset search uses),
    only the document's current pass, in reading order, flagged for a model
    to judge. Empty when `document_id` is not set.
    """
    if not document_id:
        return []

    from documents.search import service as search_service

    queryset, _ = search_service.authorized_chunks(user)
    chunks = (
        queryset.filter(document_version__document_id=document_id)
        .exclude(id__in=list(taken_chunk_ids))
        .select_related("document_version__document")
        .defer(*search_service.CANDIDATE_DEFERRED_FIELDS)
        .order_by("chunk_index")[:limit]
    )

    items = []
    for chunk in chunks:
        text = (chunk.text or "").strip()
        if len(text) < MIN_CHARS or not _HAS_LETTER.search(text):
            continue
        items.append(
            RerankedChunk(
                chunk=chunk,
                score=0.0,
                signals={"model_judged": True, "leading": True},
            )
        )
    return items


def neighbours(user, anchors, taken_chunk_ids, *, limit=None) -> list:
    """The chunks just before and after the most relevant passages.

    A list, a table of conditions or a procedure that runs over a chunk
    boundary reaches the model as two halves of an answer, and the half
    that was not retrieved is simply missing. These are the chunks either
    side of the top `limit` anchors, in the same document and pass.

    They are real chunks, so a citation to one is checked like any other.
    They are fetched through the same authorized queryset as search (a
    neighbour of a chunk you may read is in a document you may read, and that
    is not left to assumption), flagged `model_judged` so a quote never uses
    them, and returned after every anchor so they only fill the budget the
    anchors left.
    """
    limit = (
        getattr(settings, "RAG_NEIGHBOUR_ANCHORS", DEFAULT_NEIGHBOUR_ANCHORS)
        if limit is None
        else limit
    )

    wanted = {}
    for item in anchors[:limit]:
        chunk = item.chunk
        content_id = getattr(chunk, "document_content_id", None)
        index = getattr(chunk, "chunk_index", None)
        if content_id is None or index is None:
            continue
        for step, side in ((-1, "before"), (1, "after")):
            key = (content_id, index + step)
            if index + step >= 0 and key not in wanted:
                wanted[key] = (chunk.id, side)
    if not wanted:
        return []

    from documents.search import service as search_service

    condition = Q()
    for content_id, index in wanted:
        condition |= Q(document_content_id=content_id, chunk_index=index)

    queryset, _ = search_service.authorized_chunks(user)
    rows = {
        (row.document_content_id, row.chunk_index): row
        for row in queryset.filter(condition)
        .exclude(id__in=list(taken_chunk_ids))
        .select_related("document_version__document")
        .defer(*search_service.CANDIDATE_DEFERRED_FIELDS)
    }

    items = []
    for key, (anchor_id, side) in wanted.items():
        row = rows.get(key)
        if row is None:
            continue
        text = (row.text or "").strip()
        if len(text) < MIN_CHARS or not _HAS_LETTER.search(text):
            continue
        items.append(
            RerankedChunk(
                chunk=row,
                score=0.0,
                signals={
                    "model_judged": True,
                    "neighbour_of": anchor_id,
                    "side": side,
                },
            )
        )
    return items
