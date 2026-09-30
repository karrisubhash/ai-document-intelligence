"""Prometheus metrics for the categories in spec §20.

Two rules shape what is in here.

*Label values are bounded.* Every label is a stage name, a status, an error
code or a model name - values from a fixed vocabulary. A raw URL path, a
document id or a user id as a label would create a new time series per
document, which is how a metrics backend gets taken down by its own
instrumentation. Ids belong in logs, where they are cheap and searchable;
labels are for dimensions you group by.

*Nothing here carries document content.* A metric can say that OCR failed on
four pages; it can never say what the pages said. The same rule the logging
module enforces applies structurally here, because the only thing a counter
can hold is a number.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# A private registry rather than the global default, so importing this module
# twice under a test runner cannot raise "duplicated timeseries" and so the
# exposed surface is exactly what this project declares.
#
# One registry per process. Under gunicorn that is a problem, because
# there are several: with three workers, a scrape is answered by whichever
# one accepted the connection and reports roughly a third of the traffic.
# The number is not noisy, it is WRONG, and it is wrong in the flattering
# direction - error counts and latencies all read low.
#
# `PROMETHEUS_MULTIPROC_DIR` fixes that: each process writes its samples to
# a shared directory and `render()` aggregates across all of them. See
# render() and backend/gunicorn.conf.py, which is where dead workers get
# reaped so a restarted worker's counters are not double-counted forever.
#
# The Celery workers are a different matter and deliberately so: they are
# separate CONTAINERS, and the right answer there is a scrape target per
# container, which is what Prometheus expects. Their pipeline telemetry is
# also on stdout as structured JSON regardless.
REGISTRY = CollectorRegistry()

# Buckets in seconds, tuned for what this system actually does: sub-second
# API calls, multi-second OCR, and a long tail for big scanned PDFs. The
# default Prometheus buckets top out at 10s and would put every slow OCR in
# one +Inf bucket, which hides exactly the latency worth seeing.
FAST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SLOW_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
RATIO_BUCKETS = (0.0, 0.1, 0.25, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0)


# --- API (§20: request count, errors, P95/P99 latency, throughput) ----------

http_requests = Counter(
    "adi_http_requests_total",
    "HTTP requests handled.",
    # `route` is the URL PATTERN, never the resolved path - /api/documents/7/
    # and /api/documents/8/ are one series, not two.
    ["method", "route", "status"],
    registry=REGISTRY,
)

http_duration = Histogram(
    "adi_http_request_duration_seconds",
    "Wall time per request.",
    ["method", "route"],
    buckets=FAST_BUCKETS,
    registry=REGISTRY,
)

throttled_requests = Counter(
    "adi_throttled_requests_total",
    "Requests rejected by a rate limit.",
    ["route"],
    registry=REGISTRY,
)


# --- Workers and documents --------------------------------------------------

pipeline_stage_duration = Histogram(
    "adi_pipeline_stage_duration_seconds",
    "Wall time per pipeline stage.",
    ["stage", "status"],
    buckets=SLOW_BUCKETS,
    registry=REGISTRY,
)

pipeline_stages = Counter(
    "adi_pipeline_stages_total",
    "Pipeline stage outcomes.",
    ["stage", "status"],
    registry=REGISTRY,
)

pipeline_jobs = Counter(
    "adi_pipeline_jobs_total",
    "Processing jobs by final outcome.",
    # QUARANTINED is deliberately its own outcome, not folded into failed -
    # see the document status model.
    ["outcome"],
    registry=REGISTRY,
)

pipeline_failures = Counter(
    "adi_pipeline_failures_total",
    "Pipeline failures by structured error code.",
    ["stage", "error_code", "retryable"],
    registry=REGISTRY,
)

pages_processed = Counter(
    "adi_pages_processed_total",
    "Pages extracted, by the method that produced the text.",
    ["extraction_method"],
    registry=REGISTRY,
)

documents_indexed = Counter(
    "adi_documents_indexed_total",
    "Documents that completed indexing.",
    ["embedded"],
    registry=REGISTRY,
)

queue_depth = Gauge(
    "adi_queue_depth",
    "Tasks waiting in the broker queue.",
    ["queue"],
    registry=REGISTRY,
    # Queue depth is a property of the BROKER, not of the process that
    # read it, so every worker reports the same number and summing them
    # would multiply the backlog by the worker count. Take the most
    # recently written value from a live process.
    multiprocess_mode="livemostrecent",
)


# --- RAG (§20: retrieval latency, candidate count, context size) ------------

rag_requests = Counter(
    "adi_rag_requests_total",
    "RAG questions answered or abstained.",
    ["abstained"],
    registry=REGISTRY,
)

rag_duration = Histogram(
    "adi_rag_duration_seconds",
    "End-to-end time for one RAG question.",
    buckets=FAST_BUCKETS,
    registry=REGISTRY,
)

rag_candidates = Histogram(
    "adi_rag_candidates",
    "Candidates returned by retrieval before reranking.",
    buckets=(0, 1, 2, 5, 10, 20, 50, 100),
    registry=REGISTRY,
)

rag_passages_used = Histogram(
    "adi_rag_passages_used",
    "Passages that reached the context window.",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10),
    registry=REGISTRY,
)

rag_abstentions = Counter(
    "adi_rag_abstentions_total",
    "Abstentions by reason. The reasons are a fixed vocabulary, so this is"
    " safe as a label and is the single most useful RAG signal to alert on.",
    ["reason"],
    registry=REGISTRY,
)

rag_groundedness = Histogram(
    "adi_rag_groundedness",
    "Support ratio of answers that were returned.",
    buckets=RATIO_BUCKETS,
    registry=REGISTRY,
)


# --- LLM (§20 and NFR-10: model, latency, tokens, cost) ---------------------

llm_requests = Counter(
    "adi_llm_requests_total",
    "Answerer invocations.",
    ["model", "generative", "abstained"],
    registry=REGISTRY,
)

llm_tokens = Counter(
    "adi_llm_tokens_total",
    "Tokens reported by the provider. The bundled extractive answerer has"
    " none to report and contributes zero rather than an estimate.",
    ["model", "kind"],
    registry=REGISTRY,
)

llm_characters = Counter(
    "adi_llm_characters_total",
    "Prompt and completion characters. Recorded because the bundled"
    " answerer has no tokenizer, and a character count is an honest proxy"
    " where a token count would be a guess.",
    ["model", "kind"],
    registry=REGISTRY,
)

# Per-stage time inside one RAG question. `stage` is a fixed vocabulary
# (retrieval, rerank, generation, grounding), so the label is bounded. The
# end-to-end histogram above says a question was slow; this says WHICH
# part, which is the only thing an operator can act on.
rag_stage_duration = Histogram(
    "adi_rag_stage_duration_seconds",
    "Time spent in one stage of a RAG question.",
    ["stage"],
    buckets=FAST_BUCKETS,
    registry=REGISTRY,
)

# Labelled by the configured answerer's name, which is one value per
# deployment - never by question, user or document.
llm_duration = Histogram(
    "adi_llm_duration_seconds",
    "Wall time of one answerer call, including any provider retries.",
    ["model"],
    buckets=FAST_BUCKETS,
    registry=REGISTRY,
)

# A generative answerer that quietly degrades to the extractive baseline
# still returns a 200 and a plausible answer, so without a counter the
# outage is invisible. `reason` is an exception CLASS name - a bounded set
# (timeout, rate limit, auth, not configured), never a message.
llm_fallbacks = Counter(
    "adi_llm_fallbacks_total",
    "Generative answers that fell back to the extractive baseline.",
    ["answerer", "reason"],
    registry=REGISTRY,
)

rerank_degraded = Counter(
    "adi_rerank_degraded_total",
    "Rerank calls whose configured cross-encoder failed and were scored"
    " lexically instead.",
    ["reranker"],
    registry=REGISTRY,
)


# --- Quality (§20: faithfulness, citation accuracy, user feedback) ----------

answer_feedback = Counter(
    "adi_answer_feedback_total",
    "Human ratings on answers.",
    ["rating"],
    registry=REGISTRY,
)

evaluation_metric = Gauge(
    "adi_evaluation_metric",
    "Latest value of an offline evaluation metric, by dataset and metric"
    " name. Exposed so AI quality sits on the same dashboard as uptime.",
    ["dataset", "metric"],
    # Also a global fact rather than a per-process one.
    multiprocess_mode="livemostrecent",
    registry=REGISTRY,
)


# --- Security (§20: authn failures, authz failures, suspicious inputs) ------

auth_failures = Counter(
    "adi_auth_failures_total",
    "Failed authentication attempts.",
    registry=REGISTRY,
)

authz_denials = Counter(
    "adi_authz_denials_total",
    "Authorization denials, by the action that was refused.",
    ["action"],
    registry=REGISTRY,
)

suspicious_inputs = Counter(
    "adi_suspicious_inputs_total",
    "Inputs that tripped a defensive check.",
    ["kind"],
    registry=REGISTRY,
)

malware_verdicts = Counter(
    "adi_malware_verdicts_total",
    "Scanner verdicts on uploaded files.",
    ["verdict"],
    registry=REGISTRY,
)

upload_rejections = Counter(
    "adi_upload_rejections_total",
    "Uploads refused at the API boundary, before anything was stored.",
    # A fixed vocabulary from documents.uploads: empty, too_large,
    # unsupported_extension. Never the filename or the extension itself -
    # an attacker picks those, and a label they control is a label that
    # can be made unbounded.
    ["reason"],
    registry=REGISTRY,
)


def multiprocess_dir() -> str:
    """The shared sample directory, or "" when not running multiprocess."""
    import os

    return os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")


def render() -> tuple:
    """(payload, content_type) for the /metrics endpoint.

    Under gunicorn the samples of every worker are collected from the
    shared directory into a throwaway registry, so one scrape describes the
    whole container rather than whichever worker happened to answer it.

    A fresh registry per scrape is correct and not wasteful: the
    MultiProcessCollector reads the files at collection time, so reusing a
    registry would pin the first scrape's view of which processes existed.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    directory = multiprocess_dir()
    if not directory:
        return generate_latest(REGISTRY), CONTENT_TYPE_LATEST

    from prometheus_client import multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=directory)
    return generate_latest(registry), CONTENT_TYPE_LATEST
