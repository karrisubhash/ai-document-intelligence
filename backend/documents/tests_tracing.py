"""Tracing (spec 20) must cost nothing and change nothing when it is off."""

import os
from unittest import mock

from django.test import SimpleTestCase

from documents.observability import tracing


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

    def test_span_and_traced_are_transparent(self):
        with tracing.span("x", a=1) as current:
            self.assertIsNone(current)

        @tracing.traced("y")
        def add(a, b):
            return a + b

        self.assertEqual(add(2, b=3), 5)
        tracing.record_rag_answer(None, object())  # tolerated, not inspected


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
