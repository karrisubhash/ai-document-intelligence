"""Distributed tracing with OpenTelemetry (spec 20, 26; NFR-06).

Off unless an OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT, or the
traces-only OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) AND the optional layer is
installed (backend/requirements-otel.txt, image build arg INSTALL_OTEL=true).
Either missing, every function here is a no-op, so the default deployment
carries no tracing dependency and pays no per-request cost. The exporter is
OTLP over HTTP (port 4318), not gRPC.

When on, one trace follows a request through the Django view, its SQL, the
Celery task it queued and the worker's SQL, plus the RAG stages recorded
below. The request's span carries its X-Request-ID as `adi.request_id`, so a
log line or an error report leads to its trace. Spans carry ids, counts and
timings - never document text, questions or answers (spec 13: no
confidential content in ordinary telemetry) - and URLs without their query
string. Health checks and metrics scrapes are not traced.

Configured per PROCESS, after any fork: gunicorn imports the WSGI module in
each worker (no preload_app), and Celery calls this from worker_process_init.
A BatchSpanProcessor started before a fork would export from a thread the
child does not have.
"""

import functools
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_tracer = None

# Probes, not traffic: Prometheus scrapes every 15 s and Docker's
# healthcheck runs as often, so traced they outnumbered real requests on a
# quiet pilot. OTEL_PYTHON_DJANGO_EXCLUDED_URLS (or OTEL_PYTHON_EXCLUDED_URLS)
# replaces the list.
UNTRACED_URLS = "/metrics$,/health/$,/ready/$"

# Where the Django instrumentation puts the URL, under the old and the stable
# semantic conventions.
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full")


def _without_query(span, request):
    """request_hook: a span records the path, never the query string.

    The query is where search terms live - a document title, a person's name
    or email address (`?search=`) - which is why the request middleware does
    not log the path at all. Exported whole, every listing search reached
    the collector.
    """
    if span is None or not span.is_recording():
        return
    attributes = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])
    if attributes.get("url.query"):
        span.set_attribute("url.query", "")


def _with_request_id(span, request, response):
    """response_hook: the X-Request-ID the middleware gave this request."""
    from .context import RESPONSE_HEADER

    if span is None or not span.is_recording():
        return
    request_id = response.get(RESPONSE_HEADER) if response is not None else None
    if request_id:
        span.set_attribute("adi.request_id", request_id)


def _sampler():
    """Every trace, except one that would be a lone database query.

    A query outside any request or task - the health check's SELECT 1 (its
    request is not traced), connection set-up, a management command - would
    otherwise be a trace of its own, one per probe. Queries inside a traced
    request or task follow their parent as before. OTEL_TRACES_SAMPLER, when
    set, replaces this.
    """
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_ON,
        Decision,
        ParentBased,
        Sampler,
        SamplingResult,
    )
    from opentelemetry.trace import SpanKind

    class NoLoneQueries(Sampler):
        def should_sample(
            self,
            parent_context,
            trace_id,
            name,
            kind=None,
            attributes=None,
            links=None,
            trace_state=None,
        ):
            if kind == SpanKind.CLIENT:
                return SamplingResult(Decision.DROP)
            return ALWAYS_ON.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state
            )

        def get_description(self):
            return "NoLoneQueries"

    return ParentBased(root=NoLoneQueries())


def configure_tracing(service_name: str) -> bool:
    """Install the exporter and auto-instrumentation. True if tracing is on."""
    global _tracer
    if _tracer is not None:
        return True
    # The traces-only variable counts too: the exporter honours it, so a
    # setup using only it exported nothing while this stayed silently off.
    if not (
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.celery import CeleryInstrumentor
        from opentelemetry.instrumentation.django import DjangoInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OpenTelemetry layer is "
            "not installed (backend/requirements-otel.txt); tracing stays off"
        )
        return False

    from django.conf import settings

    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
            # Defaulted from DEBUG: nothing sets DEPLOY_ENVIRONMENT in the
            # production overlay, and its traces were all tagged development.
            "deployment.environment": os.getenv("DEPLOY_ENVIRONMENT")
            or ("development" if settings.DEBUG else "production"),
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=None if os.getenv("OTEL_TRACES_SAMPLER") else _sampler(),
    )
    # The exporter reads the endpoint, headers and timeout from the standard
    # OTEL_EXPORTER_OTLP_* variables.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    DjangoInstrumentor().instrument(
        excluded_urls=os.getenv("OTEL_PYTHON_DJANGO_EXCLUDED_URLS")
        or os.getenv("OTEL_PYTHON_EXCLUDED_URLS")
        or UNTRACED_URLS,
        request_hook=_without_query,
        response_hook=_with_request_id,
    )
    CeleryInstrumentor().instrument()
    # Statement text is parameterised SQL - values are bound separately - so
    # no document content reaches a span through it.
    PsycopgInstrumentor().instrument(enable_commenter=False)

    _tracer = trace.get_tracer("documents")
    logger.info("tracing enabled", extra={"service": service_name})
    return True


def enabled() -> bool:
    return _tracer is not None


def flush() -> None:
    """Export the spans still waiting in the batch processor, now.

    For a process that is about to end without running atexit handlers: a
    Celery child exits through os._exit(), so a recycled or stopped child
    lost its last few seconds of spans - its final task among them.
    """
    if _tracer is None:
        return
    from opentelemetry import trace

    force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if force_flush is not None:
        force_flush()


@contextmanager
def span(name: str, **attributes):
    """A child span when tracing is on; nothing at all when it is off."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def traced(name: str):
    """Decorator form of span(), for whole functions."""

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if _tracer is None:
                return func(*args, **kwargs)
            with span(name):
                return func(*args, **kwargs)

        return wrapper

    return decorate


def record_rag_answer(current, answer) -> None:
    """Counts and stage timings of one answer - never its text."""
    if current is None:
        return
    current.set_attribute("rag.abstained", bool(answer.abstained))
    current.set_attribute("rag.abstention_reason", answer.abstention_reason or "")
    current.set_attribute("rag.retrieved", answer.retrieved)
    current.set_attribute("rag.reranked", answer.reranked)
    current.set_attribute("rag.passages_used", answer.passages_used)
    current.set_attribute("rag.citations", len(answer.citations))
    current.set_attribute("rag.support_ratio", float(answer.support_ratio))
    current.set_attribute("rag.suspected_injections", answer.suspected_injections)
    current.set_attribute("rag.model", answer.model_name or "")
    current.set_attribute("rag.reranker", answer.reranker_name or "")
    current.set_attribute("rag.reranker_degraded", bool(answer.reranker_degraded))
    current.set_attribute("rag.answerer_fell_back", bool(answer.answerer_fell_back))
    for stage, ms in (answer.timings or {}).items():
        current.set_attribute(f"rag.stage_ms.{stage}", ms)
