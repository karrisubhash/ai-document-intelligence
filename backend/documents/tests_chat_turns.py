"""A chat turn's lifecycle: live progress, Stop, and Regenerate / Edit."""

import time
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import Conversation, Message
from documents.rag import budget, engine
from documents.tests import make_user
from documents.tests_chat_streaming import StreamingChatClient, chunk, events_of
from documents.tests_providers import PASSAGE, FakeSearchResponse, answerer_with
from documents.tests_rag import chunk as passage
from documents.tests_rag import hits

CHAT = "/api/v1/chat/"
STREAM = "/api/v1/chat/stream/"
QUESTION = "What is the minimum attendance for the examination?"


def over_one_passage(test, answerer):
    """The real engine over one passage of "Regulations", with `answerer`."""
    for patch in (
        mock.patch.object(
            engine.search_service,
            "search",
            return_value=FakeSearchResponse(
                hits((passage(1, PASSAGE, title="Regulations"), 1.0))
            ),
        ),
        mock.patch.object(engine, "get_answerer", return_value=answerer),
        mock.patch.object(engine, "_figure_hints", return_value=[]),
    ):
        patch.start()
        test.addCleanup(patch.stop)


def groq(client):
    return answerer_with(client, RAG_ANSWERER_PROVIDER="groq", RAG_ANSWERER_MODEL="")


class EndlessStream(StreamingChatClient):
    """Streams a word every few milliseconds until it is closed."""

    def __init__(self):
        super().__init__(["x"])
        self.closed = False
        self.sent = 0

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        client = self

        def words():
            try:
                while True:
                    client.sent += 1
                    yield chunk("word ")
                    time.sleep(0.005)
            finally:
                client.closed = True

        return words()


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class ProgressEventTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        budget.reset()
        self.client = APIClient()
        self.client.force_authenticate(make_user("watcher"))

    def stream(self):
        response = self.client.post(
            STREAM,
            {"question": QUESTION},
            format="json",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return events_of(response)

    def test_stages_and_sources_arrive_before_the_answer(self):
        over_one_passage(
            self, groq(StreamingChatClient(["You need ", "75 percent [1]."]))
        )
        events = self.stream()
        names = [name for name, _ in events]

        stages = [data["stage"] for name, data in events if name == "status"]
        self.assertEqual(stages, ["retrieving", "ranking", "writing", "verifying"])
        self.assertLess(names.index("sources"), names.index("delta"))
        self.assertEqual(names[-1], "done")

        [sources] = [data for name, data in events if name == "sources"]
        self.assertEqual(
            sources["sources"],
            [
                {
                    "marker": 1,
                    "document_id": 1,
                    "document_title": "Regulations",
                    "page_number": 1,
                }
            ],
        )
        # Titles and pages only: the passage text is the done event's to carry.
        self.assertNotIn("75 percent", str(sources))


@mock.patch.dict("os.environ", {"GROQ_API_KEY": "k"})
class StopTests(TransactionTestCase):
    """TransactionTestCase: the turn runs on its own thread and connection."""

    def setUp(self):
        cache.clear()
        budget.reset()
        self.client = APIClient()
        self.client.force_authenticate(make_user("stopper"))

    def test_a_reader_who_leaves_stops_the_generation_and_nothing_is_saved(self):
        provider = EndlessStream()
        over_one_passage(self, groq(provider))
        response = self.client.post(
            STREAM,
            {"question": QUESTION},
            format="json",
            HTTP_ACCEPT="text/event-stream",
        )
        content = iter(response.streaming_content)
        # Read until the model has started writing, as a reader would.
        while b"event: delta" not in next(content):
            pass

        response.close()  # what the server does when the connection is gone
        response.worker.join(timeout=10)

        self.assertFalse(response.worker.is_alive())
        self.assertTrue(provider.closed, "the provider stream was left running")
        sent = provider.sent
        time.sleep(0.05)
        self.assertEqual(provider.sent, sent)
        self.assertEqual(Message.objects.count(), 0)
        # The conversation was made for this question and is gone with it.
        self.assertEqual(Conversation.objects.count(), 0)

    def test_an_answer_that_finished_is_kept(self):
        over_one_passage(self, groq(StreamingChatClient(["You need 75 percent [1]."])))
        response = self.client.post(
            STREAM,
            {"question": QUESTION},
            format="json",
            HTTP_ACCEPT="text/event-stream",
        )
        events = events_of(response)
        response.worker.join(timeout=10)
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(Message.objects.count(), 2)


class ReplaceTurnTests(TestCase):
    def setUp(self):
        self.user = make_user("editor")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def ask(self, question, **extra):
        return self.client.post(CHAT, {"question": question, **extra}, format="json")

    def test_a_regenerated_answer_replaces_the_turn_it_was_asked_for(self):
        first = self.ask("What is the hostel fee?").data
        second = self.ask(
            "What is the hostel fee, per semester?",
            conversation_id=first["conversation_id"],
            replaces=first["message_id"],
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)
        messages = list(
            Message.objects.filter(conversation_id=first["conversation_id"]).order_by(
                "created_at", "id"
            )
        )
        self.assertEqual(
            [m.content for m in messages if m.role == Message.Role.USER],
            ["What is the hostel fee, per semester?"],
        )
        self.assertEqual(len(messages), 2)

    def test_only_an_answer_of_this_conversation_can_be_replaced(self):
        mine = self.ask("first").data
        other = APIClient()
        other.force_authenticate(make_user("someone-else"))
        theirs = other.post(CHAT, {"question": "theirs"}, format="json").data
        question_id = Message.objects.get(
            conversation_id=mine["conversation_id"], role=Message.Role.USER
        ).id

        for replaces in (theirs["message_id"], question_id, 999999):
            with self.subTest(replaces=replaces):
                response = self.ask(
                    "again", conversation_id=mine["conversation_id"], replaces=replaces
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Nothing was removed by the refused attempts.
        self.assertEqual(
            Message.objects.filter(conversation__owner=self.user).count(), 2
        )

    def test_replacing_needs_a_conversation(self):
        response = self.ask("again", replaces=1)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_stream_refuses_a_bad_replacement_before_streaming(self):
        mine = self.ask("first").data
        response = self.client.post(
            STREAM,
            {
                "question": "again",
                "conversation_id": mine["conversation_id"],
                "replaces": 999999,
            },
            format="json",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response["Content-Type"], "application/json")
