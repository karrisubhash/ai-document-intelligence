"""Passage-level scoring for the Search endpoint.

The chat benchmarks score DOCUMENTS after reranking, through `rag.ask`. The
Search page shows PASSAGES, in the order the search endpoint returns them,
and nothing measured that order: a page whose first result was an OCR
fragment and whose second was the right clause scored exactly like one that
had them the other way round, because both "retrieved the document".

A passage is relevant to a case when it belongs to a labelled document and
contains that label's evidence text. Labelling by evidence rather than by
chunk id keeps a dataset valid across a change of chunk size or overlap -
chunk ids are specific to one database and one chunking run.

Like `metrics`, nothing here imports Django: these are the definitions every
search number is measured against.
"""

import re
from dataclasses import dataclass, field

from .metrics import ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank

WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Lower-cased, whitespace collapsed: what evidence matching compares."""
    return WHITESPACE.sub(" ", (text or "")).strip().lower()


def contains_evidence(passage: str, evidence: str) -> bool:
    return normalise(evidence) in normalise(passage)


def evidence_recall_at_k(ranked: list, evidence: list, k: int) -> float:
    """Share of the evidence items that some passage in the top k carries.

    Counted per evidence item, not per relevant chunk: chunks overlap, so one
    clause is often carried by two adjacent chunks, and a page that showed
    the clause once has not missed half of it.
    """
    if not evidence or k <= 0:
        return 0.0
    top = set(ranked[:k])
    return sum(1 for chunks in evidence if chunks & top) / len(evidence)


@dataclass
class CaseScore:
    case_id: str
    kind: str
    split: str
    query: str
    answerable: bool
    returned: int
    recall: float = 0.0
    precision: float = 0.0
    reciprocal_rank: float = 0.0
    ndcg: float = 0.0
    document_recall: float = 0.0
    document_reciprocal_rank: float = 0.0
    # Whether the evidence was among the candidates retrieval produced at
    # all. Separates "never found" from "found and ranked badly", which need
    # different fixes.
    pool_recall: float = 0.0
    outcome: str = ""
    distinct_documents: int = 0
    most_from_one_document: int = 0
    duration_ms: int = 0
    top: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            key: (round(value, 4) if isinstance(value, float) else value)
            for key, value in self.__dict__.items()
        }


# Outcomes, from best to worst. "ranking" means retrieval found the
# evidence and the ordering (or a filter after it) kept it out of the top k;
# "retrieval" means it was never a candidate.
TOP1 = "top1"
TOP_K = "top-k"
RANKING = "ranking"
RETRIEVAL = "retrieval"
CORRECT_EMPTY = "correct-empty"
RESULTS_FOR_NOTHING = "results-for-no-relevant"


def score_case(
    case: dict,
    ranked: list,
    documents: dict,
    evidence: list,
    pool: set,
    *,
    k: int,
    duration_ms: int = 0,
    titles: dict | None = None,
) -> CaseScore:
    """Score one query.

    `ranked` is chunk ids in returned order; `documents` maps each chunk id
    to its document id; `evidence` is one set of chunk ids per evidence item
    (every current chunk that carries it); `pool` is the chunk ids retrieval
    produced before any ordering was applied.
    """
    relevant_chunks = set().union(*evidence) if evidence else set()
    relevant_documents = {documents[chunk] for chunk in relevant_chunks}

    ranked_documents = []
    for chunk in ranked:
        if documents[chunk] not in ranked_documents:
            ranked_documents.append(documents[chunk])

    top = ranked[:k]
    per_document = {}
    for chunk in top:
        per_document[documents[chunk]] = per_document.get(documents[chunk], 0) + 1

    score = CaseScore(
        case_id=case["id"],
        kind=case.get("kind", ""),
        split=case.get("split", ""),
        query=case["query"],
        answerable=bool(evidence),
        returned=len(ranked),
        distinct_documents=len(per_document),
        most_from_one_document=max(per_document.values(), default=0),
        duration_ms=duration_ms,
        top=[
            {
                "chunk": chunk,
                "document": (titles or {}).get(documents[chunk], documents[chunk]),
                "relevant": chunk in relevant_chunks,
            }
            for chunk in top
        ],
    )

    if not evidence:
        score.outcome = CORRECT_EMPTY if not ranked else RESULTS_FOR_NOTHING
        return score

    score.recall = evidence_recall_at_k(ranked, evidence, k)
    score.precision = precision_at_k(ranked, relevant_chunks, k)
    score.reciprocal_rank = reciprocal_rank(ranked, relevant_chunks)
    score.ndcg = ndcg_at_k(ranked, relevant_chunks, k)
    score.document_recall = recall_at_k(ranked_documents, relevant_documents, k)
    score.document_reciprocal_rank = reciprocal_rank(
        ranked_documents, relevant_documents
    )
    score.pool_recall = sum(1 for chunks in evidence if chunks & pool) / len(evidence)

    if ranked and ranked[0] in relevant_chunks:
        score.outcome = TOP1
    elif relevant_chunks & set(top):
        score.outcome = TOP_K
    elif relevant_chunks & pool:
        score.outcome = RANKING
    else:
        score.outcome = RETRIEVAL
    return score


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(values, fraction: float) -> int:
    values = sorted(values)
    if not values:
        return 0
    index = min(len(values) - 1, max(0, round(fraction * (len(values) - 1))))
    return values[index]


def aggregate(scores: list, k: int) -> dict:
    """Headline numbers for a set of cases.

    `quality` holds only metrics where higher is better, so a regression
    check can compare it key by key; `diagnostics` holds counts and
    latencies, which describe a run without being better or worse in one
    direction.
    """
    answerable = [s for s in scores if s.answerable]
    unanswerable = [s for s in scores if not s.answerable]

    quality = {}
    if answerable:
        quality.update(
            {
                f"recall_at_{k}": _mean(s.recall for s in answerable),
                f"precision_at_{k}": _mean(s.precision for s in answerable),
                "mrr": _mean(s.reciprocal_rank for s in answerable),
                f"ndcg_at_{k}": _mean(s.ndcg for s in answerable),
                f"document_recall_at_{k}": _mean(s.document_recall for s in answerable),
                "document_mrr": _mean(s.document_reciprocal_rank for s in answerable),
                "pool_recall": _mean(s.pool_recall for s in answerable),
                # An answerable query that came back empty: the cost side of
                # any relevance cut-off.
                "answerable_with_results": _mean(
                    1.0 if s.returned else 0.0 for s in answerable
                ),
            }
        )
    if unanswerable:
        quality["no_relevant_returned_nothing"] = _mean(
            1.0 if not s.returned else 0.0 for s in unanswerable
        )

    outcomes = {}
    for s in scores:
        outcomes[s.outcome] = outcomes.get(s.outcome, 0) + 1

    diagnostics = {
        "cases": len(scores),
        "answerable": len(answerable),
        "no_relevant": len(unanswerable),
        "outcomes": dict(sorted(outcomes.items())),
        "mean_returned_answerable": _mean(s.returned for s in answerable),
        "mean_returned_no_relevant": _mean(s.returned for s in unanswerable),
        f"mean_distinct_documents_at_{k}": _mean(
            s.distinct_documents for s in answerable
        ),
        "latency_p50_ms": _percentile([s.duration_ms for s in scores], 0.5),
        "latency_p95_ms": _percentile([s.duration_ms for s in scores], 0.95),
    }

    return {
        "quality": {key: round(value, 4) for key, value in quality.items()},
        "diagnostics": {
            key: (round(value, 4) if isinstance(value, float) else value)
            for key, value in diagnostics.items()
        },
    }
