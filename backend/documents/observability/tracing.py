"""Distributed tracing with OpenTelemetry (spec 20, 26; NFR-06).

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set AND the optional layer is
installed (backend/requirements-otel.txt, image build arg INSTALL_OTEL=true).
Either missing, every function here is a no-op, so the default deployment
carries no tracing dependency and pays no per-request cost.

When on, one trace follows a request from nginx's X-Request-ID through the
Django view, its SQL, the Celery task it queued and the worker's SQL, plus
the RAG stages recorded below. Spans carry ids, counts and timings - never
document text, questions or answers (spec 13: no confidential content in
ordinary telemetry).

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


def configure_tracing(service_name: str) -> bool:
    """Install the exporter and auto-instrumentation. True if tracing is on."""
    global _tracer
    if _tracer is not None:
        return True
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
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

    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
            "deployment.environment": os.getenv("DEPLOY_ENVIRONMENT", "development"),
        }
    )
    provider = TracerProvider(resource=resource)
    # The exporter reads the endpoint, headers and timeout from the standard
    # OTEL_EXPORTER_OTLP_* variables.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    DjangoInstrumentor().instrument()
    CeleryInstrumentor().instrument()
    # Statement text is parameterised SQL - values are bound separately - so
    # no document content reaches a span through it.
    PsycopgInstrumentor().instrument(enable_commenter=False)

    _tracer = trace.get_tracer("documents")
    logger.info("tracing enabled", extra={"service": service_name})
    return True


def enabled() -> bool:
    return _tracer is not None


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
