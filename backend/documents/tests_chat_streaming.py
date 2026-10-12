"""Streamed chat (POST /api/v1/chat/stream/) and general-knowledge replies."""

import json
import time
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import Message
from documents.rag import engine, followup, general, openai_compat, streaming
from documents.rag.engine import Abstention
from documents.tests import make_user
from documents.tests_providers import (
    PASSAGE,
    FakeSearchResponse,
    StubChatClient,
    answerer_with,
    completion,
)
from documents.tests_rag import chunk as passage
from documents.tests_rag import hits

CHAT = "/api/v1/chat/"
STREAM = "/api/v1/chat/stream/"
GENERAL_ON = {"CHAT_GENERAL_KNOWLEDGE": True}


def chunk(text=None, finish=None):
    return SimpleNamespace(
        model="stub-model",
        usage=None,
        choices=[
            SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)
        ],
    )


class StreamingChatClient(StubChatClient):
    """Streams `pieces` when asked to stream, else returns them whole."""

    def __init__(self, pieces):
        super().__init__(completion="".join(pieces) and completion("".join(pieces)))
        self.pieces = pieces

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if kwargs.get("stream"):
            return iter([*(chunk(p) for p in self.pieces), chunk(None, "stop")])
        return self.completion


def stub_answerer(pieces=("Hello! ", "How can I help?"), raises=None):
    client = StreamingChatClient(list(pieces))
    client.raises = raises
    return answerer_with(client, RAG_ANSWERER_PROVIDER="groq", GROQ_API_KEY="k")


def events_of(response):
    body = b"".join(response.streaming_content).decode()
    events = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


class CompleteStreamsThroughTheSinkTests(SimpleTestCase):
    def test_deltas_reach_the_sink_and_the_whole_text_is_returned(self):
        seen = []
        with (
            mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}),
            streaming.capture(lambda event, text: seen.append((event, text))),
        ):
            result = stub_answerer(["Hel", "lo"]).complete(
                {"model": "m", "messages": []}
            )
        self.assertEqual(seen, [("delta", "Hel"), ("delta", "lo")])
        self.assertEqual(result.text, "Hello")
        self.assertTrue(result.streamed)

    def test_without_a_sink_nothing_streams(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}):
            answerer = stub_answerer(["Hello"])
            result = answerer.complete({"model": "m", "messages": []})
        self.assertFalse(result.streamed)
        self.assertNotIn("stream", answerer._client.calls[0])


class GeneralKnowledgeRulesTests(SimpleTestCase):
    def test_never_after_a_grounding_failure_or_a_figure_hint(self):
        for reason in (Abstention.UNGROUNDED, Abstention.MAYBE_IN_A_FIGURE):
            with self.subTest(reason=reason):
                self.assertNotIn(reason, general.FALLBACK_REASONS)

    def test_never_after_the_answerer_failed(self):
        # A cut-off or refused answer, or an outage: the passages may well
        # hold the answer, and the general reply would say they do not.
        self.assertNotIn(Abstention.ANSWERER_FAILED, general.FALLBACK_REASONS)

    @override_settings(**GENERAL_ON)
    def test_needs_a_generative_answerer(self):
        # The test settings pin the extractive answerer, which has no
        # knowledge of its own to answer from.
        self.assertFalse(general.enabled())


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class GeneralKnowledgeChatTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(make_user("chatter"))

    def ask(self, question="hi"):
        response = self.client.post(CHAT, {"question": question}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    @override_settings(**GENERAL_ON)
    def test_small_talk_gets_a_labelled_uncited_reply(self):
        with mock.patch(
            "documents.rag.general.get_answerer", return_value=stub_answerer()
        ):
            data = self.ask()
        self.assertEqual(data["source"], "general")
        self.assertFalse(data["abstained"])
        self.assertEqual(data["answer"], "Hello! How can I help?")
        self.assertEqual(data["citations"], [])
        stored = Message.objects.get(id=data["message_id"])
        self.assertEqual(stored.metadata["source"], "general")

    def test_off_by_default_the_chat_abstains(self):
        with mock.patch(
            "documents.rag.general.get_answerer", return_value=stub_answerer()
        ):
            data = self.ask()
        self.assertEqual(data["source"], "none")
        self.assertTrue(data["abstained"])

    @override_settings(**GENERAL_ON)
    def test_a_provider_failure_keeps_the_abstention(self):
        failing = stub_answerer(raises=RuntimeError("429"))
        with mock.patch("documents.rag.general.get_answerer", return_value=failing):
            data = self.ask()
        self.assertEqual(data["source"], "none")
        self.assertTrue(data["abstained"])

    @override_settings(**GENERAL_ON)
    def test_earlier_turns_are_sent_as_context(self):
        answerer = stub_answerer()
        with mock.patch("documents.rag.general.get_answerer", return_value=answerer):
            first = self.ask("my name is Asha")
            self.client.post(
                CHAT,
                {
                    "question": "what is my name?",
                    "conversation_id": first["conversation_id"],
                },
                format="json",
            )
        messages = answerer._client.calls[-1]["messages"]
        self.assertIn("my name is Asha", [m["content"] for m in messages])


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class ChatStreamEndpointTests(TransactionTestCase):
    """TransactionTestCase: the turn runs in a worker thread, on its own
    database connection, which cannot see an uncommitted test transaction."""

    def setUp(self):
        self.user = make_user("streamer")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def stream(self, payload, accept="text/event-stream"):
        # The header a browser's fetch() sends. Without it these tests
        # passed while every real request was refused with a 406.
        response = self.client.post(STREAM, payload, format="json", HTTP_ACCEPT=accept)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        return events_of(response)

    def test_extractive_answer_arrives_as_one_done_event(self):
        events = self.stream({"question": "hi"})
        self.assertEqual([name for name, _ in events], ["done"])
        self.assertEqual(events[0][1]["source"], "none")
        self.assertEqual(Message.objects.count(), 2)

    @override_settings(**GENERAL_ON)
    def test_generated_words_stream_before_the_authoritative_done(self):
        with mock.patch(
            "documents.rag.general.get_answerer", return_value=stub_answerer()
        ):
            events = self.stream({"question": "hello there"})
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "done")
        deltas = "".join(data["text"] for name, data in events if name == "delta")
        self.assertEqual(deltas, "Hello! How can I help?")
        self.assertEqual(events[-1][1]["answer"], deltas)
        self.assertEqual(events[-1][1]["source"], "general")

    def test_invalid_request_is_a_json_400_not_a_stream(self):
        response = self.client.post(
            STREAM, {"question": ""}, format="json", HTTP_ACCEPT="text/event-stream"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_every_accept_header_a_client_sends_is_served(self):
        for accept in (
            "text/event-stream",
            "text/event-stream, application/json",
            "*/*",
            "application/json",
        ):
            with self.subTest(accept=accept):
                names = [name for name, _ in self.stream({"question": "hi"}, accept)]
                self.assertEqual(names[-1], "done")

    def test_an_anonymous_browser_gets_a_json_401_not_a_406(self):
        anonymous = APIClient()
        response = anonymous.post(
            STREAM, {"question": "hi"}, format="json", HTTP_ACCEPT="text/event-stream"
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(response["Content-Type"], "application/json")

    def test_someone_elses_conversation_is_a_404_before_streaming(self):
        other = APIClient()
        other.force_authenticate(make_user("other"))
        theirs = other.post(CHAT, {"question": "hi"}, format="json").data[
            "conversation_id"
        ]
        response = self.client.post(
            STREAM, {"question": "hi", "conversation_id": theirs}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class PerModelClient(StubChatClient):
    """Fails the models in `failing` with their status code; answers the rest."""

    def __init__(self, failing):
        super().__init__(completion=completion("Answer [1]."))
        self.failing = failing

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] in self.failing:
            raise StatusError(self.failing[kwargs["model"]])
        return self.completion


@mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"})
class GeminiModelChainTests(SimpleTestCase):
    def setUp(self):
        openai_compat.reset_cooldowns()
        self.addCleanup(openai_compat.reset_cooldowns)

    def gemini(self, client, **overrides):
        settings = {
            "RAG_ANSWERER_PROVIDER": "gemini",
            "RAG_ANSWERER_MODEL": "",
            "RAG_ANSWERER_BASE_URL": "",
            **overrides,
        }
        return answerer_with(client, **settings)

    def request(self, answerer):
        return {"model": answerer.model, "messages": [{"role": "user", "content": "q"}]}

    def test_a_rate_limited_model_moves_to_the_next_free_model(self):
        answerer = self.gemini(PerModelClient({"gemini-3.5-flash": 429}))
        result = answerer.complete(self.request(answerer))
        self.assertEqual(result.text, "Answer [1].")
        self.assertEqual(
            [call["model"] for call in answerer._client.calls],
            ["gemini-3.5-flash", "gemini-3-flash-preview"],
        )

    def test_overloaded_and_retired_models_are_skipped_too(self):
        answerer = self.gemini(
            PerModelClient({"gemini-3.5-flash": 503, "gemini-3-flash-preview": 404})
        )
        answerer.complete(self.request(answerer))
        self.assertEqual(answerer._client.calls[-1]["model"], "gemini-3.7-flash")

    def test_a_bad_key_is_not_retried_on_other_models(self):
        answerer = self.gemini(PerModelClient({"gemini-3.5-flash": 401}))
        with self.assertRaises(StatusError):
            answerer.complete(self.request(answerer))
        self.assertEqual(len(answerer._client.calls), 1)

    def test_when_every_model_is_limited_the_last_error_surfaces(self):
        answerer = self.gemini(PerModelClient({}))
        everything = [answerer.model, *answerer.fallback_models]
        answerer._client = PerModelClient(dict.fromkeys(everything, 429))
        with self.assertRaises(StatusError):
            answerer.complete(self.request(answerer))
        self.assertEqual([c["model"] for c in answerer._client.calls], everything)

    def test_an_overloaded_model_is_benched_briefly(self):
        self.assertEqual(openai_compat._cooldown_seconds(StatusError(503)), 30.0)

    def test_a_pinned_model_has_no_chain(self):
        answerer = self.gemini(
            PerModelClient({"gemini-3.7-flash": 429}),
            RAG_ANSWERER_MODEL="gemini-3.7-flash",
        )
        self.assertEqual(answerer.fallback_models, ())

    def test_thinking_is_off_for_gemini_unless_configured_otherwise(self):
        answerer = self.gemini(PerModelClient({}))
        self.assertEqual(answerer._request_kwargs("p")["reasoning_effort"], "none")
        default = self.gemini(
            PerModelClient({}), RAG_ANSWERER_REASONING_EFFORT="default"
        )
        self.assertNotIn("reasoning_effort", default._request_kwargs("p"))

    def test_other_presets_send_no_extras(self):
        with mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"}):
            groq = answerer_with(PerModelClient({}), RAG_ANSWERER_PROVIDER="groq")
        self.assertNotIn("reasoning_effort", groq._request_kwargs("p"))
        self.assertEqual(groq.fallback_models, ())

    def test_a_rate_limited_model_is_tried_last_on_the_next_call(self):
        client = PerModelClient({"gemini-3.5-flash": 429})
        answerer = self.gemini(client)
        answerer.complete(self.request(answerer))
        client.calls.clear()
        answerer.complete(self.request(answerer))
        self.assertEqual([c["model"] for c in client.calls], ["gemini-3-flash-preview"])

    def test_a_bad_retry_after_cannot_break_the_chain(self):
        error = StatusError(429)
        error.response = SimpleNamespace(
            headers={"retry-after": "Sun, 11 Oct 2026 15:30:00 GMT"}
        )
        self.assertEqual(openai_compat._cooldown_seconds(error), 60.0)

    def test_gemini_retry_hint_and_daily_quota_set_the_cooldown(self):
        self.assertEqual(
            openai_compat._cooldown_seconds(Exception("Please retry in 14.48s.")), 14.48
        )
        self.assertEqual(
            openai_compat._cooldown_seconds(
                Exception("quotaId: ...PerDayPerProject...")
            ),
            3600.0,
        )

    def test_no_model_in_the_chain_is_one_that_rejects_the_extras(self):
        # The -lite models answer reasoning_effort "none" with a 400, which
        # ends the chain; with the others cooling it was the model tried.
        answerer = self.gemini(PerModelClient({}))
        self.assertEqual(answerer.request_extras.get("reasoning_effort"), "none")
        self.assertEqual(
            [model for model in answerer.fallback_models if "lite" in model], []
        )

    def test_an_answer_records_the_model_that_actually_answered(self):
        answerer = self.gemini(PerModelClient({"gemini-3.5-flash": 429}))
        response = answerer._generate(question="q", context=None, prompt="p")
        self.assertEqual(response.model_version, "gemini/gemini-3-flash-preview")

    @override_settings(**GENERAL_ON)
    def test_a_general_reply_records_the_model_that_actually_answered(self):
        answerer = self.gemini(PerModelClient({"gemini-3.5-flash": 503}))
        with mock.patch("documents.rag.general.get_answerer", return_value=answerer):
            reply = general.answer("hi")
        self.assertEqual(reply.model_version, "gemini/gemini-3-flash-preview")


class SmallTalkTests(SimpleTestCase):
    def test_greetings_and_thanks_are_small_talk(self):
        for text in (
            "hi!",
            "Hiii",
            "hello there",
            "Hey, how are you?",
            "thanks so much!",
        ):
            with self.subTest(text=text):
                self.assertTrue(followup.is_small_talk(text))

    def test_questions_are_not(self):
        for text in (
            "hi, what is the fee?",
            "Is it valid?",
            "thanks for the fee details",
        ):
            with self.subTest(text=text):
                self.assertFalse(followup.is_small_talk(text))

    def test_a_greeting_is_never_rewritten_as_a_follow_up(self):
        # Found live: "hi!" after an attendance question was widened into
        # the attendance question and answered with the policy again.
        self.assertEqual(followup.looks_dependent("hi!"), (False, ""))

    def test_every_way_of_saying_thank_you_still_counts(self):
        for text in ("thank you", "Thank you so much!", "ok thank you all"):
            with self.subTest(text=text):
                self.assertTrue(followup.is_small_talk(text))

    def test_matching_takes_linear_time(self):
        # "thank you" was also "thank" + "you", so every repetition doubled
        # the paths a failing match tried: 24 of them took tens of seconds,
        # and the 2,000 characters a question may have held a worker for good.
        started = time.monotonic()
        self.assertIsNone(followup._SMALL_TALK.match("thank you " * 24 + "x"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_long_message_is_never_small_talk(self):
        # The cap bounds the regex's work whatever phrase is added later.
        self.assertTrue(followup.is_small_talk("hi " * 10))
        self.assertFalse(followup.is_small_talk("hi " * 11))


class BreakingStreamClient(StreamingChatClient):
    """Streams the first piece, then loses the connection."""

    def _create(self, **kwargs):
        self.calls.append(kwargs)

        def broken():
            yield chunk(self.pieces[0])
            raise ConnectionError("connection reset")

        return broken()


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class WithdrawnDraftTests(TestCase):
    """Only `done` is authoritative, but until it arrives a client shows the
    deltas. A draft the engine does not return must be withdrawn, or the
    reader watches an unverified answer, citation markers and all."""

    def stream_engine(self, answerer):
        events = []
        found = FakeSearchResponse(hits((passage(1, PASSAGE), 1.0)))
        with (
            mock.patch.object(engine.search_service, "search", return_value=found),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
            streaming.capture(lambda event, text: events.append(event)),
        ):
            answer = engine.ask(
                None, "What is the minimum attendance for the examination?"
            )
        return answer, events

    def test_an_ungrounded_draft_is_withdrawn(self):
        answerer = stub_answerer(["The minimum attendance is ", "80 percent [1]."])
        answer, events = self.stream_engine(answerer)
        self.assertEqual(answer.abstention_reason, Abstention.UNGROUNDED)
        self.assertEqual(events, ["delta", "delta", "reset"])

    def test_a_stream_that_breaks_is_withdrawn_before_the_quoted_answer(self):
        client = BreakingStreamClient(["The minimum attendance "])
        answerer = answerer_with(client, RAG_ANSWERER_PROVIDER="groq")
        answer, events = self.stream_engine(answerer)
        self.assertTrue(answer.answerer_fell_back)
        self.assertEqual(events, ["delta", "reset"])


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class SmallTalkChatTests(TestCase):
    @override_settings(**GENERAL_ON)
    def test_a_greeting_skips_document_search(self):
        client = APIClient()
        client.force_authenticate(make_user("greeter"))
        with (
            mock.patch(
                "documents.rag.general.get_answerer", return_value=stub_answerer()
            ),
            mock.patch("documents.agents.answer") as searched,
        ):
            data = client.post(CHAT, {"question": "hi!"}, format="json").data
        searched.assert_not_called()
        self.assertEqual(data["source"], "general")
        self.assertIn("small talk; documents not searched", data["notes"])
