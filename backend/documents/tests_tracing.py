"""Tracing (spec 20) must cost nothing and change nothing when it is off."""

import importlib.util
import os
import re
from unittest import mock, skipUnless

from django.test import SimpleTestCase

from documents.observability import tracing

# The optional layer (requirements-otel.txt); absent from the default image.
# The parent is checked first: find_spec() of a submodule raises, rather
# than answering None, when its package is missing.
OTEL_INSTALLED = (
    importlib.util.find_spec("opentelemetry") is not None
    and importlib.util.find_spec("opentelemetry.sdk") is not None
)


class TracingOffTests(SimpleTestCase):
    def test_no_endpoint_means_no_tracing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            self.assertFalse(tracing.configure_tracing("test"))
        self.assertFalse(tracing.enabled())

    def test_endpoint_without_the_layer_stays_off(self):
        with (
            mock.patch.dict(
                os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4318"}
            ),
            mock.patch.dict("sys.modules", {"opentelemetry": None}),
            self.assertLogs("documents.observability.tracing", "WARNING"),
        ):
            self.assertFalse(tracing.configure_tracing("test"))

    def test_the_traces_only_endpoint_turns_it_on_too(self):
        # The exporter honours it, so a setup that set only this one
        # exported nothing while tracing stayed silently off.
        with (
            mock.patch.dict(
                os.environ,
                {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://x:4318/v1/traces"},
            ),
            mock.patch.dict("sys.modules", {"opentelemetry": None}),
            self.assertLogs("documents.observability.tracing", "WARNING"),
        ):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            self.assertFalse(tracing.configure_tracing("test"))

    def test_flush_is_a_no_op_when_off(self):
        tracing.flush()

    def test_span_and_traced_are_transparent(self):
        with tracing.span("x", a=1) as current:
            self.assertIsNone(current)

        @tracing.traced("y")
        def add(a, b):
            return a + b

        self.assertEqual(add(2, b=3), 5)
        tracing.record_rag_answer(None, object())  # tolerated, not inspected


class RequestSpanHookTests(SimpleTestCase):
    """What the Django instrumentation records, as the hooks rewrite it."""

    def recording_span(self, **attributes):
        span = mock.Mock()
        span.is_recording.return_value = True
        span.attributes = dict(attributes)
        span.set_attribute.side_effect = span.attributes.__setitem__
        return span

    def test_urls_are_exported_without_their_query_string(self):
        # `?search=` carries document titles and people's names; the
        # instrumentation recorded it whole in http.url and http.target.
        span = self.recording_span(
            **{
                "http.url": "http://host/api/v1/admin/users/?search=jane%40example.org",
                "http.target": "/api/v1/documents/?search=Jane+Doe&page=2",
                "url.query": "search=Jane+Doe",
            }
        )
        tracing._without_query(span, request=None)
        self.assertEqual(span.attributes["http.url"], "http://host/api/v1/admin/users/")
        self.assertEqual(span.attributes["http.target"], "/api/v1/documents/")
        self.assertEqual(span.attributes["url.query"], "")

    def test_a_url_without_a_query_is_left_alone(self):
        span = self.recording_span(**{"http.target": "/api/v1/documents/"})
        tracing._without_query(span, request=None)
        span.set_attribute.assert_not_called()

    def test_the_request_id_goes_on_the_span(self):
        span = self.recording_span()
        tracing._with_request_id(span, None, {"X-Request-ID": "abc123"})
        self.assertEqual(span.attributes["adi.request_id"], "abc123")

    def test_probes_are_not_traced(self):
        patterns = [re.compile(p) for p in tracing.UNTRACED_URLS.split(",")]
        for url in ("http://h/metrics", "http://h/health/", "http://h/ready/"):
            with self.subTest(url=url):
                self.assertTrue(any(p.search(url) for p in patterns))
        self.assertFalse(any(p.search("http://h/api/v1/documents/") for p in patterns))


@skipUnless(OTEL_INSTALLED, "the optional OpenTelemetry layer is not installed")
class SamplerTests(SimpleTestCase):
    def test_a_lone_query_is_dropped_and_everything_else_kept(self):
        # The untraced health check's SELECT 1 became a trace of its own,
        # one per probe; a query inside a traced request follows its parent.
        from opentelemetry.sdk.trace.sampling import Decision
        from opentelemetry.trace import SpanKind

        sampler = tracing._sampler()
        self.assertEqual(
            sampler.should_sample(None, 1, "SELECT", SpanKind.CLIENT).decision,
            Decision.DROP,
        )
        for kind in (SpanKind.SERVER, SpanKind.CONSUMER, SpanKind.INTERNAL):
            with self.subTest(kind=kind):
                self.assertEqual(
                    sampler.should_sample(None, 1, "work", kind).decision,
                    Decision.RECORD_AND_SAMPLE,
                )


class RagSpanAttributesTests(SimpleTestCase):
    """The attributes recorded are counts and timings, never text."""

    def test_records_no_question_answer_or_citation_text(self):
        from documents.rag.engine import RagAnswer

        answer = RagAnswer(
            question="secret question",
            answer="secret answer",
            abstained=False,
            citations=[{"text": "secret passage"}],
            timings={"retrieval": 12},
        )
        recorded = {}
        current = mock.Mock()
        current.set_attribute.side_effect = recorded.__setitem__
        tracing.record_rag_answer(current, answer)

        self.assertEqual(recorded["rag.citations"], 1)
        self.assertEqual(recorded["rag.stage_ms.retrieval"], 12)
        self.assertNotIn("secret", " ".join(str(v) for v in recorded.values()))
