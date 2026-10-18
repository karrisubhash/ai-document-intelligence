"""Ordering and selection for the Search endpoint.

`service.search` produces CANDIDATES: the permission filter, then lexical and
vector retrieval fused by reciprocal rank. Reciprocal-rank fusion is the
right way to merge two rankings whose scores are not comparable, but it has
no notion of how relevant anything is - only of where each retriever put it.
Two consequences, both observed on a real OCR-heavy corpus, and both what
the Search page used to show directly:

*Disjoint lists interleave.* When the retrievers disagree, the first place
from each scores the same 1/61, so the page alternated between them. "library
late return fine" put an OCR fragment reading "Final Results Deadline: I 2" -
first in the dense retriever's list, absent from the lexical one - above the
clause that answers it, which was first in the lexical list.

*There is always a full page.* The vector half returns its nearest
SEARCH_CANDIDATES chunks however far away they are, and the lexical half ORs
the query's terms so that one shared word is a match. A query nothing in the
corpus is about ("quantum chromodynamics lattice gauge theory") returned ten
passages of a mechanics textbook that mentions "theory".

The fix is the step chat already had: a cross-encoder reads the query and
each candidate together and reorders them, and passages it scores below a
relevance floor are left off the page. Nothing here can add a candidate - a
reranker only reorders what the permission-filtered retrieval returned - so
the authorization property of `service.search` carries through unchanged.

WHAT WAS MEASURED, AND WHAT IT DECIDED (docs/evaluation/search-relevance.md;
every choice below was made on the benchmark's dev split, never its test
split):

* ONLY A CROSS-ENCODER REORDERS. The bundled lexical reranker made the order
  WORSE than fusion in every configuration measured (dev MRR 0.870 -> 0.742
  with bge-small embeddings, 0.844 -> 0.733 with hashing): it cannot see a
  passage that answers in other words, and fusion's vector half can. So with
  the lexical reranker configured - or a cross-encoder that fell back to it
  - the fused order is returned as before.

* THE SEARCH FLOOR IS NOT THE CHAT FLOOR. MiniLM's chat floor (0.30) hid the
  answer to every paraphrased dev query: on a question worded unlike its
  answer, MiniLM scores the right passage about as low as junk (0.0000 -
  0.08), so no floor separates "paraphrase" from "nothing relevant". The
  search floors below were chosen by a rule fixed before the sweep: maximise
  (no-relevant queries returning nothing) - 2 x (answerable queries whose
  every relevant passage is hidden), ties to the lower floor. They still hide
  some paraphrase answers, which is why below-floor passages stay one request
  away (`include_below_floor`) rather than being dropped.

* NEVER AN EMPTY PAGE WHEN THERE WERE CANDIDATES. With nothing above the
  floor, the SEARCH_FALLBACK_RESULTS best-ranked passages are returned,
  marked below_floor, beside a note that none is a close match. This one was
  decided AFTER the test split was scored, and says so: the strict version
  left 9 of 13 paraphrased answers off the page behind "no close match",
  although MiniLM still ranked them first to third. A floor cannot fix that
  (see above); returning the closest few, labelled, does - and a query no
  document answers still gets an explicit no-match response, not a page of
  unrelated passages presented as results.

* AN UNCALIBRATED CROSS-ENCODER REORDERS BUT HIDES NOTHING. A floor belongs
  to one model's score distribution; applying a guess would hide passages on
  a number nobody measured. SEARCH_RELEVANCE_FLOOR sets one explicitly.
"""

import logging

from django.conf import settings

from documents.observability import telemetry, tracing
from documents.rag import engine

from . import service

logger = logging.getLogger("adi.search")

# How many fused candidates the cross-encoder reads (at least `limit`). It
# can only promote what it is shown, so this bounds recall; it is also the
# per-search CPU cost. On the benchmark the first relevant passage sat at
# fused rank 7 or better whenever retrieval found it at all.
DEFAULT_RERANK_DEPTH = 30

# Search relevance floors per cross-encoder model (the reranker's `version`
# is its model id). Chosen on the dev split of
# evaluation/search-relevance.template.json, summed over the synthetic
# corpus and the same corpus plus 2,000 QS-OCR scans; see the module
# docstring for the rule and docs/evaluation/search-relevance.md for the
# sweep. Re-measure when the model changes.
CALIBRATED_FLOORS = {
    "cross-encoder/ms-marco-MiniLM-L6-v2": 0.03,
}

# When nothing clears the floor, how many of the best-ranked passages are
# still returned, marked below_floor. 3 because every dev paraphrase had its
# answer at reranked rank 1-3. 0 returns an empty list instead.
DEFAULT_FALLBACK_RESULTS = 3


def fallback_results() -> int:
    return max(
        0, int(getattr(settings, "SEARCH_FALLBACK_RESULTS", DEFAULT_FALLBACK_RESULTS))
    )


def rerank_depth() -> int:
    """SEARCH_RERANK_DEPTH; 0 turns reranking off (the fused order is kept)."""
    return max(0, int(getattr(settings, "SEARCH_RERANK_DEPTH", DEFAULT_RERANK_DEPTH)))


def relevance_floor(reranker) -> float | None:
    """SEARCH_RELEVANCE_FLOOR, else the model's calibrated search floor.

    None means no floor: results are reordered, and none is left out.
    """
    configured = getattr(settings, "SEARCH_RELEVANCE_FLOOR", None)
    if configured is not None:
        return configured
    return CALIBRATED_FLOORS.get(getattr(reranker, "version", ""))


@tracing.traced("search.ranked")
def search(
    user,
    query: str,
    *,
    limit: int = 10,
    include_below_floor: bool = False,
    **filters,
) -> service.SearchResponse:
    """What the Search endpoint returns: retrieve, rerank, apply the floor."""
    depth = rerank_depth()
    reranker = engine.get_reranker() if depth else None

    if reranker is None or not getattr(reranker, "semantic", False):
        return service.search(user, query, limit=limit, **filters)

    # Never fewer candidates than the caller asked to see: "show more" must
    # not stop at the rerank depth while retrieval had more.
    response = service.search(user, query, limit=max(limit, depth), **filters)
    if not response.hits:
        return response

    return rank(
        response,
        query,
        reranker,
        limit=limit,
        include_below_floor=include_below_floor,
    )


def rank(
    response: service.SearchResponse,
    query: str,
    reranker,
    *,
    limit: int,
    include_below_floor: bool = False,
) -> service.SearchResponse:
    reranked = reranker.rerank(query, response.hits)
    response.reranker = f"{reranker.name} {getattr(reranker, 'version', '')}".strip()

    if engine.was_degraded(reranked):
        # The fallback's scores are lexical, and lexical reordering measured
        # worse than the fused order it would replace - so keep that order.
        telemetry.rerank_degraded(reranker.name)
        response.reranker_degraded = True
        response.notes.append(
            f"reranker {reranker.name} was unavailable; results are in "
            f"retrieval order, unfiltered"
        )
        response.hits = response.hits[:limit]
        return response

    floor = relevance_floor(reranker)
    by_chunk = {hit.chunk.id: hit for hit in response.hits}

    above, below = [], []
    for item in reranked:
        hit = by_chunk[item.chunk.id]
        hit.relevance = item.score
        hit.below_floor = floor is not None and item.score < floor
        (below if hit.below_floor else above).append(hit)

    response.relevance_floor = floor
    response.reranked = len(reranked)
    response.below_floor = len(below)

    if include_below_floor:
        selected = above + below
    elif above:
        selected = above
    else:
        # Nothing cleared the floor. The closest few, marked, rather than an
        # empty page: the floor cannot tell a paraphrased answer from no
        # answer, and the reranker's ORDER still puts the answer first.
        selected = below[: fallback_results()]

    response.hits = selected[:limit]

    if below and not above:
        shown = len(response.hits)
        response.notes.append(
            f"no passage is a close enough match: all {len(below)} scored below "
            f"the relevance floor (best {reranked[0].score:.3f}, floor "
            f"{floor:.3f}); "
            + (
                f"the {shown} closest are shown, marked as weaker matches"
                if shown
                else "none is shown"
            )
        )

    logger.info(
        "search.ranked",
        extra={
            "reranker": reranker.name,
            "candidates": len(reranked),
            "above_floor": len(above),
            "returned": len(response.hits),
        },
    )
    return response
