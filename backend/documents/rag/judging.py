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

from . import gateway as gateway_module
from .reranking import RerankedChunk

DEFAULT_MIN_PASSAGES = 6
DEFAULT_RETRIEVAL_TOP = 2

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


def model_can_judge(answerer) -> bool:
    """Whether a generative model is expected to write this answer.

    `generative is True` rather than truthiness: a test double's attributes
    are all truthy. And a gateway with no link ready returns False, so the
    answer that is about to be a quote is not handed passages a quote cannot
    judge. The check reads the budget and sends nothing.
    """
    if not enabled() or getattr(answerer, "generative", False) is not True:
        return False

    gateway = getattr(answerer, "gateway", None)
    if not isinstance(gateway, gateway_module.Gateway):
        # An answerer with no budget to consult (the Claude answerer).
        return True

    try:
        return bool(gateway.available(gateway_module.current_purpose()))
    except Exception:  # noqa: BLE001 - a budget outage must not decide answers
        # The gateway makes its own decision when the call is made, and the
        # quoting fallback never sees model_judged passages either way.
        return True


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
