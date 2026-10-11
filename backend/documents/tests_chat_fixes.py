"""Regressions found by running the chat in a real browser (2026-10-11).

Each test pins one confirmed finding of the live review: the short answer
passage the context builder threw away, the general-knowledge reply that
claimed the documents had nothing, the cold dev server, the streamed turn
that lost its request id and its failure count, and the label missing while
a general answer streamed.
"""

import json
import os
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from documents import apps as documents_apps
from documents.observability import context as obs_context
from documents.observability import metrics
from documents.processing import warmup
from documents.rag import context as ctx
from documents.rag import engine, general
from documents.rag.engine import Abstention
from documents.rag.llm import ExtractiveAnswerer
from documents.tests import make_user
from documents.tests_chat_streaming import STREAM, events_of, stub_answerer
from documents.tests_rag import chunk, hits

POLICY = "Attendance policy: minimum 75 percent."  # 38 chars: a whole document


def candidates(*texts):
    return hits(*[(chunk(i + 1, t), 0.9) for i, t in enumerate(texts)])


class ShortEvidenceTests(SimpleTestCase):
    def test_a_short_passage_a_cross_encoder_judged_relevant_is_kept(self):
        built = ctx.build(candidates(POLICY), semantic_scores=True)
        self.assertEqual([p.text for p in built.passages], [POLICY])
        self.assertEqual(built.dropped_as_tiny, 0)

    def test_letterless_debris_is_still_dropped_with_semantic_scores(self):
        built = ctx.build(candidates("12 / 3.36 -- 7"), semantic_scores=True)
        self.assertEqual(built.passages, [])
        self.assertEqual(built.dropped_as_tiny, 1)

    def test_lexical_scores_keep_the_length_guard(self):
        # Lexical density inflates a tiny passage's score; length is the
        # only guard there, so the old rule stands.
        built = ctx.build(candidates(POLICY))
        self.assertEqual(built.passages, [])


class EngineShortEvidenceTests(TestCase):
    """TestCase: an abstention looks up figure hints in the database."""

    def ask_with(self, reranker):
        search = mock.Mock(hits=hits((chunk(1, POLICY), 0.0)), semantic=True, notes=[])
        with (
            mock.patch.object(engine.search_service, "search", return_value=search),
            mock.patch.object(engine, "get_reranker", return_value=reranker),
            mock.patch.object(
                engine, "get_answerer", return_value=ExtractiveAnswerer()
            ),
        ):
            return engine.ask(None, "attendance policy minimum percent")

    def cross_encoder(self, *, degraded=False):
        class Scored:
            name = "stub-cross-encoder"
            semantic_scores = True
            default_relevance_floor = 0.3

            def rerank(self, question, retrieved):
                return [
                    SimpleNamespace(
                        chunk=item.chunk,
                        score=0.46,
                        signals={"reranker_degraded": degraded},
                    )
                    for item in retrieved
                ]

        return Scored()

    def test_the_engine_uses_a_short_top_passage_from_a_cross_encoder(self):
        answer = self.ask_with(self.cross_encoder())
        self.assertNotEqual(answer.abstention_reason, Abstention.EMPTY_CONTEXT)
        self.assertEqual(answer.passages_used, 1)

    def test_an_empty_context_says_why_in_its_notes(self):
        # Degraded to lexical scores, the length guard applies again, so the
        # 38-character passage is dropped - and the notes must say so.
        answer = self.ask_with(self.cross_encoder(degraded=True))
        self.assertEqual(answer.abstention_reason, Abstention.EMPTY_CONTEXT)
        self.assertIn(
            "context construction dropped all 1 passage(s) above the floor: "
            "1 too short, 0 duplicate, 0 over budget",
            answer.notes,
        )


class GeneralKnowledgeNeverAfterAnEmptiedContextTests(SimpleTestCase):
    def test_empty_context_is_not_a_fallback_reason(self):
        # Passages cleared the floor and were then dropped: the documents
        # DO have evidence, so "they had nothing" would be untrue.
        self.assertNotIn(Abstention.EMPTY_CONTEXT, general.FALLBACK_REASONS)


class DevServerWarmupTests(SimpleTestCase):
    def setUp(self):
        documents_apps._dev_warmup_started = False
        self.addCleanup(setattr, documents_apps, "_dev_warmup_started", False)

    def test_warms_once_per_serving_process(self):
        with (
            mock.patch.object(warmup, "enabled", return_value=True),
            mock.patch("documents.apps.threading.Thread") as thread,
        ):
            documents_apps.warm_dev_server()
            documents_apps.warm_dev_server()
        self.assertEqual(thread.call_count, 1)
        self.assertIs(thread.call_args.kwargs["target"], warmup.warm)

    def test_never_under_tests(self):
        with mock.patch("documents.apps.threading.Thread") as thread:
            documents_apps.warm_dev_server()
        thread.assert_not_called()

    def test_connected_to_the_reloader_child_signal_only(self):
        from django.utils.autoreload import autoreload_started

        self.assertIn(
            "documents.warm_dev_server",
            [key[0][0] for key in autoreload_started.receivers],
        )


class AnswererWarmupTests(SimpleTestCase):
    def test_a_hosted_answerer_builds_its_client_without_a_request(self):
        answerer = mock.Mock(spec=["_get_client"])
        with mock.patch.object(engine, "get_answerer", return_value=answerer):
            loaded = warmup.warm(reason="test", components=("answerer",))
        answerer._get_client.assert_called_once_with()
        self.assertIsInstance(loaded["answerer"], float)

    def test_the_extractive_answerer_has_nothing_to_warm(self):
        with mock.patch.object(
            engine, "get_answerer", return_value=ExtractiveAnswerer()
        ):
            self.assertEqual(warmup.warm(reason="test", components=("answerer",)), {})


@mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"})
class StreamedTurnTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user("streamed-fixes"))

    def post(self):
        return self.client.post(
            STREAM,
            {"question": "hello there"},
            format="json",
            HTTP_ACCEPT="text/event-stream",
        )

    @override_settings(CHAT_GENERAL_KNOWLEDGE=True)
    def test_a_general_reply_is_labelled_while_it_streams(self):
        with mock.patch(
            "documents.rag.general.get_answerer", return_value=stub_answerer()
        ):
            events = events_of(self.post())
        resets = [data for name, data in events if name == "reset"]
        self.assertEqual(resets[0], {"source": "general"})
        first_delta = next(i for i, (name, _) in enumerate(events) if name == "delta")
        first_reset = next(i for i, (name, _) in enumerate(events) if name == "reset")
        self.assertLess(first_reset, first_delta)

    def test_the_worker_thread_keeps_the_request_id(self):
        seen = []

        def spy(*args, **kwargs):
            seen.append(obs_context.get_request_id())
            raise RuntimeError("boom")

        with mock.patch("documents.agents.answer", side_effect=spy):
            response = self.client.post(
                STREAM,
                {"question": "what is the fee?"},
                format="json",
                HTTP_ACCEPT="text/event-stream",
                HTTP_X_REQUEST_ID="req-abc123",
            )
            events_of(response)
        self.assertEqual(seen, ["req-abc123"])

    def test_a_failed_stream_is_counted(self):
        before = metrics.chat_stream_failures._value.get()
        with mock.patch("documents.agents.answer", side_effect=RuntimeError("boom")):
            events = events_of(
                self.client.post(
                    STREAM,
                    {"question": "what is the fee?"},
                    format="json",
                    HTTP_ACCEPT="text/event-stream",
                )
            )
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(metrics.chat_stream_failures._value.get(), before + 1)
        self.assertNotIn("boom", json.dumps(events))  # no internals leaked
