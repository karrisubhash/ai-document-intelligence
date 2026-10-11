"""Streamed chat (POST /api/v1/chat/stream/) and general-knowledge replies."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import Message
from documents.rag import general, streaming
from documents.rag.engine import Abstention
from documents.tests import make_user
from documents.tests_providers import StubChatClient, answerer_with, completion

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

    def stream(self, payload):
        response = self.client.post(STREAM, payload, format="json")
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
        response = self.client.post(STREAM, {"question": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

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
