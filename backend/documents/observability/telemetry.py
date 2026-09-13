"""Where the rest of the system reports what it did.

One function per event, rather than callers touching Prometheus collectors
directly. That keeps label vocabularies in one place - a caller cannot
invent a new label value and quietly multiply the cardinality - and it means
every emission goes through the same redaction-safe logging path.

Every function here is deliberately total: instrumentation must never be the
reason a document fails to process. Anything that goes wrong recording a
metric is swallowed and logged rather than raised.
"""

import logging

from . import metrics
from .logging import log_event

logger = logging.getLogger("adi.telemetry")


def _safe(function):
    """Telemetry never breaks the thing it is measuring."""

    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive
            logger.warning("telemetry.failed", extra={"event": function.__name__})

    return wrapper


# --- Pipeline ---------------------------------------------------------------


@_safe
def stage_finished(stage, status, duration_seconds, *, document_id=None, job_id=None):
    metrics.pipeline_stages.labels(stage=str(stage), status=str(status)).inc()
    metrics.pipeline_stage_duration.labels(
        stage=str(stage), status=str(status)
    ).observe(duration_seconds)

    log_event(
        logger,
        logging.INFO,
        "pipeline.stage",
        stage=str(stage),
        status=str(status),
        duration_ms=round(duration_seconds * 1000, 2),
        document_id=document_id,
        job_id=job_id,
    )


@_safe
def stage_failed(stage, error_code, retryable, *, document_id=None, job_id=None):
    metrics.pipeline_failures.labels(
        stage=str(stage), error_code=str(error_code), retryable=str(bool(retryable))
    ).inc()

    log_event(
        logger,
        logging.WARNING,
        "pipeline.stage.failed",
        stage=str(stage),
        error_code=str(error_code),
        retryable=bool(retryable),
        document_id=document_id,
        job_id=job_id,
    )


@_safe
def job_finished(outcome, *, document_id=None, job_id=None, attempt=None):
    metrics.pipeline_jobs.labels(outcome=str(outcome)).inc()

    log_event(
        logger,
        logging.INFO,
        "pipeline.job",
        outcome=str(outcome),
        document_id=document_id,
        job_id=job_id,
        attempt=attempt,
    )


@_safe
def pages_extracted(counts: dict):
    """`counts` maps extraction method to how many pages used it."""
    for method, count in (counts or {}).items():
        if count:
            metrics.pages_processed.labels(extraction_method=str(method)).inc(count)


@_safe
def document_indexed(chunks, embedded, *, document_id=None):
    metrics.documents_indexed.labels(embedded=str(bool(embedded))).inc()

    log_event(
        logger,
        logging.INFO,
        "document.indexed",
        chunks=chunks,
        embedded=embedded,
        document_id=document_id,
    )


# --- RAG and the answerer ---------------------------------------------------


@_safe
def rag_answered(answer):
    """One RagAnswer, reported. Takes the object so callers cannot forget a
    field, and reads only numbers and fixed vocabularies off it."""
    metrics.rag_requests.labels(abstained=str(bool(answer.abstained))).inc()
    metrics.rag_duration.observe(answer.duration_ms / 1000.0)
    metrics.rag_candidates.observe(answer.retrieved)
    metrics.rag_passages_used.observe(answer.passages_used)

    if answer.abstained:
        metrics.rag_abstentions.labels(
            reason=str(answer.abstention_reason or "unspecified")
        ).inc()
    else:
        metrics.rag_groundedness.observe(answer.support_ratio)

    if answer.suspected_injections:
        metrics.suspicious_inputs.labels(kind="prompt_injection").inc(
            answer.suspected_injections
        )

    if answer.model_name:
        metrics.llm_requests.labels(
            model=answer.model_name,
            generative=str(bool(answer.generative)),
            abstained=str(bool(answer.abstained)),
        ).inc()

    # The question and the answer are NOT logged - see observability.logging.
    log_event(
        logger,
        logging.INFO,
        "rag.answered",
        abstained=answer.abstained,
        abstention_reason=answer.abstention_reason,
        retrieved=answer.retrieved,
        passages_used=answer.passages_used,
        support_ratio=answer.support_ratio,
        citations=len(answer.citations),
        model=answer.model_name,
        generative=answer.generative,
        duration_ms=answer.duration_ms,
    )


@_safe
def llm_usage(response):
    """Token and character usage from one answerer response (NFR-10)."""
    model = response.model_name or "unknown"

    for kind, value in (
        ("prompt", response.prompt_tokens),
        ("completion", response.completion_tokens),
    ):
        if value:
            metrics.llm_tokens.labels(model=model, kind=kind).inc(value)

    for kind, value in (
        ("prompt", response.prompt_characters),
        ("completion", response.completion_characters),
    ):
        if value:
            metrics.llm_characters.labels(model=model, kind=kind).inc(value)


# --- Security ---------------------------------------------------------------


@_safe
def authorization_denied(action, *, user_id=None, document_id=None):
    metrics.authz_denials.labels(action=str(action)).inc()

    log_event(
        logger,
        logging.WARNING,
        "security.authz.denied",
        action=str(action),
        user_id=user_id,
        document_id=document_id,
    )


@_safe
def scan_verdict(verdict, *, document_id=None, findings=0):
    metrics.malware_verdicts.labels(verdict=str(verdict)).inc()

    level = logging.WARNING if str(verdict) != "CLEAN" else logging.INFO
    log_event(
        logger,
        level,
        "security.scan",
        verdict=str(verdict),
        findings=findings,
        document_id=document_id,
    )


# --- Quality ----------------------------------------------------------------


@_safe
def feedback_recorded(rating):
    metrics.answer_feedback.labels(rating=str(rating)).inc()


@_safe
def evaluation_completed(run):
    """Publish an offline evaluation's headline numbers as gauges.

    Puts AI quality on the same dashboard as uptime, which is the point of
    §20 listing them side by side: a system can be perfectly available and
    quietly answering worse than it did last month.
    """
    dataset = f"{run.dataset.name}:{run.dataset.version}"

    for group, keys in (
        ("retrieval", ("mrr", "map")),
        (
            "generation",
            (
                "groundedness",
                "citation_accuracy",
                "source_coverage",
                "abstention_accuracy",
                "coverage",
            ),
        ),
    ):
        for key in keys:
            value = (run.metrics.get(group) or {}).get(key)
            if isinstance(value, (int, float)):
                metrics.evaluation_metric.labels(
                    dataset=dataset, metric=f"{group}.{key}"
                ).set(value)

    recall = (run.metrics.get("retrieval") or {}).get("recall_at_k") or {}
    for k, value in recall.items():
        if isinstance(value, (int, float)):
            metrics.evaluation_metric.labels(
                dataset=dataset, metric=f"retrieval.recall_at_{k}"
            ).set(value)

    log_event(
        logger,
        logging.INFO,
        "evaluation.completed",
        run_id=run.id,
        dataset=dataset,
        cases=run.cases_scored,
        duration_ms=run.duration_ms,
    )
