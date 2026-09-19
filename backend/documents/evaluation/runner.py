"""Running an evaluation dataset against the live pipeline.

Deliberately goes through `rag.ask` rather than reimplementing retrieval:
an evaluation that exercises a parallel code path measures the parallel
code path. The number is only about the system if it came out of the system.

That also means evaluation inherits the permission filter, so a benchmark is
scored as the user it runs as. A dataset whose documents that user cannot
read scores zero - which is correct, and worth seeing rather than quietly
bypassing with an escalated query.
"""

import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents import rag
from documents.models import EvaluationCaseResult, EvaluationDataset, EvaluationRun
from documents.observability import telemetry
from documents.processing.chunking import service as chunking_service
from documents.processing.chunking.exceptions import (
    EmbeddingProviderNotConfigured,
    EmbeddingProviderUnavailable,
)
from documents.processing.chunking.service import CHUNKING_STRATEGY, CHUNKING_VERSION

from . import metrics

# 1 and 3 say whether the right thing is at the top; 10 says whether it was
# found at all.
DEFAULT_KS = (1, 3, 5, 10)


def capture_configuration() -> dict:
    """Everything that could move a score, recorded with the score.

    Section 19.2 requires results to be stored with model, prompt and
    pipeline version. A metric without this is a number, not a measurement -
    there is nothing it can honestly be compared to.
    """
    try:
        provider = chunking_service.get_provider()
        embedding = {
            "provider": provider.name,
            "version": provider.version,
            "dimensions": provider.dimensions,
            "semantic": provider.semantic,
        }
    except (EmbeddingProviderNotConfigured, EmbeddingProviderUnavailable):
        # Recorded as absent rather than omitted: "ran without embeddings"
        # is a fact about the run that changes how its scores read.
        embedding = {"provider": "", "semantic": False}

    reranker = rag.engine.get_reranker()
    answerer = rag.engine.get_answerer()

    return {
        "embedding": embedding,
        "reranker": {
            "name": reranker.name,
            "version": reranker.version,
            "semantic": getattr(reranker, "semantic", False),
        },
        "answerer": {
            "name": answerer.name,
            "version": answerer.version,
            "generative": getattr(answerer, "generative", False),
        },
        "chunking": {
            "strategy": CHUNKING_STRATEGY,
            "version": CHUNKING_VERSION,
            "size": getattr(settings, "CHUNK_SIZE", None),
            "overlap": getattr(settings, "CHUNK_OVERLAP", None),
        },
        "retrieval": {
            "depth": getattr(settings, "RAG_RETRIEVAL_DEPTH", None),
            "relevance_floor": getattr(settings, "RAG_RELEVANCE_FLOOR", None),
            "candidates": getattr(settings, "SEARCH_CANDIDATES", None),
            "context_budget": getattr(settings, "RAG_CONTEXT_BUDGET", None),
            "max_passages": getattr(settings, "RAG_MAX_PASSAGES", None),
        },
    }


def score_case(answer, case, ks=DEFAULT_KS) -> dict:
    """Per-case metric values, so a bad aggregate can be traced to its cause."""
    retrieved = list(answer.retrieved_document_ids)
    cited = [citation["document_id"] for citation in answer.citations]
    relevant = [int(document_id) for document_id in case.relevant_documents]

    scores = {
        "recall_at_k": {
            str(k): round(metrics.recall_at_k(retrieved, relevant, k), 4) for k in ks
        },
        "precision_at_k": {
            str(k): round(metrics.precision_at_k(retrieved, relevant, k), 4) for k in ks
        },
        "reciprocal_rank": round(metrics.reciprocal_rank(retrieved, relevant), 4),
        "citation_accuracy": round(metrics.citation_accuracy(cited, relevant), 4),
        "source_coverage": round(metrics.source_coverage(cited, relevant), 4),
        "groundedness": answer.support_ratio,
        "abstention_correct": metrics.abstention_correct(
            answer.abstained, case.expect_abstention
        ),
    }

    if case.expected_answer:
        scores["token_overlap"] = round(
            metrics.token_overlap(answer.answer, case.expected_answer), 4
        )

    return scores


def run_dataset(
    dataset: EvaluationDataset,
    user,
    *,
    label: str = "",
    ks=DEFAULT_KS,
) -> EvaluationRun:
    """Execute every case and store per-case results plus aggregates."""
    started = time.monotonic()

    run = EvaluationRun.objects.create(
        dataset=dataset,
        label=label,
        run_by=user,
        configuration=capture_configuration(),
        cases_total=dataset.cases.count(),
    )

    retrieval_pairs = []
    generation_cases = []
    latencies = []

    try:
        for case in dataset.cases.all():
            answer = rag.ask(user, case.question)

            retrieved = list(answer.retrieved_document_ids)
            cited = [citation["document_id"] for citation in answer.citations]
            relevant = [int(document_id) for document_id in case.relevant_documents]

            EvaluationCaseResult.objects.create(
                run=run,
                case=case,
                answer=answer.answer,
                abstained=answer.abstained,
                abstention_reason=answer.abstention_reason,
                retrieved_documents=retrieved,
                cited_documents=cited,
                citations=answer.citations,
                scores=score_case(answer, case, ks=ks),
                duration_ms=answer.duration_ms,
            )

            # Cases that expect an abstention have no relevant documents, so
            # they are excluded from retrieval scoring - averaging a vacuous
            # 0.0 into recall would make a correct refusal look like a miss.
            if relevant:
                retrieval_pairs.append((retrieved, relevant))

            generation_cases.append(
                {
                    "abstained": answer.abstained,
                    "expected_abstention": case.expect_abstention,
                    "groundedness": answer.support_ratio,
                    "cited_documents": cited,
                    "relevant_documents": relevant,
                    "answer": answer.answer,
                    "expected_answer": case.expected_answer,
                }
            )
            latencies.append(answer.duration_ms)

        retrieval = metrics.aggregate_retrieval(retrieval_pairs, ks=ks)
        generation = metrics.aggregate_generation(generation_cases)

        with transaction.atomic():
            run.metrics = {
                "retrieval": retrieval.to_dict(),
                "generation": generation.to_dict(),
                "system": _system_metrics(latencies),
            }
            run.cases_scored = len(generation_cases)
            run.status = EvaluationRun.Status.COMPLETED
            run.completed_at = timezone.now()
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.save()

        telemetry.evaluation_completed(run)

    except Exception as exc:
        # A half-finished run is recorded as FAILED rather than deleted: its
        # partial results are evidence about where it broke.
        run.status = EvaluationRun.Status.FAILED
        run.error_message = str(exc)
        run.completed_at = timezone.now()
        run.duration_ms = int((time.monotonic() - started) * 1000)
        run.save()
        raise

    return run


def _system_metrics(latencies) -> dict:
    if not latencies:
        return {"cases": 0}

    ordered = sorted(latencies)
    return {
        "cases": len(ordered),
        "latency_ms_mean": round(sum(ordered) / len(ordered), 1),
        "latency_ms_p50": ordered[len(ordered) // 2],
        "latency_ms_p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "latency_ms_max": ordered[-1],
    }


def compare_runs(baseline: EvaluationRun, candidate: EvaluationRun, **kwargs) -> dict:
    """Regression check between two runs (spec §19.2)."""
    result = metrics.compare(baseline.metrics, candidate.metrics, **kwargs)
    result["baseline"] = {
        "id": baseline.id,
        "label": baseline.label,
        "configuration": baseline.configuration,
    }
    result["candidate"] = {
        "id": candidate.id,
        "label": candidate.label,
        "configuration": candidate.configuration,
    }
    result["same_dataset"] = baseline.dataset_id == candidate.dataset_id

    if not result["same_dataset"]:
        # Comparing scores from different benchmarks is comparing different
        # exams. Flagged rather than refused: an operator may genuinely want
        # the numbers side by side, as long as nobody reads the delta as a
        # change in the system.
        result["warning"] = (
            "runs are against different datasets; the deltas do not measure "
            "a change in the system"
        )

    return result
