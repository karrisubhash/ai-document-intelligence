"""Telling people which model answered, and administrators what the AI can do.

The chat payload's `model_display` and `degraded`, the provider facts stored
on each answer, GET /api/v1/chat/capabilities/, the AI section of System
status, and the per-link probe.
"""

import json
import os
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import UserProfile
from documents.chat.views import ChatCapabilitiesView
from documents.models import AuditLog, Conversation, Message
from documents.operations import health
from documents.rag import budget, engine
from documents.tests import make_user
from documents.tests_llm_gateway import NO_KEYS, ModelClient
from documents.tests_providers import PASSAGE, FakeSearchResponse, answerer_with
from documents.tests_rag import chunk, hits

Role = UserProfile.Role
CHAT = "/api/v1/chat/"
CAPABILITIES = "/api/v1/chat/capabilities/"
QUESTION = "What is the minimum attendance for the examination?"


def groq_answerer(client):
    return answerer_with(
        client,
        RAG_ANSWERER_PROVIDER="groq",
        RAG_ANSWERER_MODEL="",
        RAG_ANSWERER_BASE_URL="",
    )


class LLMTestCase(TestCase):
    def setUp(self):
        cache.clear()
        budget.reset()
        ChatCapabilitiesView._cached = (0.0, None)
        patch = mock.patch.dict(os.environ, NO_KEYS)
        patch.start()
        self.addCleanup(patch.stop)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def answered_by(self, answerer):
        """Chat runs the real engine over one passage, with this answerer."""
        for patch in (
            mock.patch.object(
                engine.search_service,
                "search",
                return_value=FakeSearchResponse(hits((chunk(1, PASSAGE), 1.0))),
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class AnswerLabelTests(LLMTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user("asker")
        self.client = self.client_for(self.user)

    def ask(self):
        response = self.client.post(CHAT, {"question": QUESTION}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_a_written_answer_names_its_model_and_keeps_what_the_provider_said(self):
        self.answered_by(groq_answerer(ModelClient(text="You need 75 percent [1].")))
        data = self.ask()

        self.assertFalse(data["abstained"], data["notes"])
        self.assertEqual(data["model_display"], "GPT-OSS 120B · Groq")
        self.assertEqual(
            data["degraded"], {"answerer": False, "reranker": False, "reason": None}
        )
        # Additive: the old field still names the answerer.
        self.assertEqual(data["model"]["name"], "openai-compatible")

        stored = Message.objects.get(id=data["message_id"])
        llm = stored.metadata["llm"]
        self.assertEqual(llm["provider"], "groq")
        self.assertEqual(llm["model"], "openai/gpt-oss-120b")
        self.assertEqual((llm["prompt_tokens"], llm["completion_tokens"]), (120, 18))
        self.assertEqual(llm["finish_reason"], "stop")
        self.assertFalse(llm["may_train_on_input"])
        self.assertEqual(llm["fell_back_from"], "")
        self.assertEqual(stored.metadata["degraded"], data["degraded"])

    def test_an_answer_quoted_at_capacity_says_so(self):
        failing = ModelClient({"openai/gpt-oss-120b": 429})
        self.answered_by(groq_answerer(failing))
        data = self.ask()

        self.assertFalse(data["abstained"])
        self.assertEqual(data["model_display"], "Quoted from your documents")
        self.assertEqual(
            data["degraded"],
            {"answerer": True, "reranker": False, "reason": "capacity"},
        )
        llm = Message.objects.get(id=data["message_id"]).metadata["llm"]
        self.assertEqual(llm["fallback_kind"], "capacity")
        self.assertEqual(llm["fell_back_from"], "groq/openai/gpt-oss-120b")
        self.assertEqual(llm["attempts"][0]["status"], 429)

    def test_an_answer_quoted_after_a_failure_is_unavailable_not_capacity(self):
        failing = ModelClient({"openai/gpt-oss-120b": TimeoutError("timed out")})
        self.answered_by(groq_answerer(failing))
        self.assertEqual(self.ask()["degraded"]["reason"], "unavailable")

    def test_a_reopened_conversation_carries_the_same_labels(self):
        self.answered_by(groq_answerer(ModelClient(text="You need 75 percent [1].")))
        data = self.ask()
        conversation = self.client.get(
            f"/api/v1/chat/conversations/{data['conversation_id']}/"
        ).data
        user, assistant = conversation["messages"]
        self.assertEqual(assistant["model_display"], "GPT-OSS 120B · Groq")
        self.assertEqual(assistant["degraded"], data["degraded"])
        self.assertEqual(user["model_display"], "")
        self.assertIsNone(user["degraded"])

    def test_answers_stored_before_the_labels_existed_get_them_derived(self):
        conversation = Conversation.objects.create(owner=self.user)
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Quoted [1]",
            model_name="extractive-answerer",
            model_version="0.1.0",
            metadata={
                "notes": [
                    "generative answerer openai-compatible was unavailable "
                    "(RateLimitError); answered by quoting passages instead"
                ]
            },
        )
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Written [1]",
            model_name="openai-compatible",
            model_version="gemini/gemini-3-flash-preview",
            generative=True,
        )
        quoted, written = self.client.get(
            f"/api/v1/chat/conversations/{conversation.id}/"
        ).data["messages"]
        self.assertEqual(quoted["model_display"], "Quoted from your documents")
        self.assertEqual(quoted["degraded"]["answerer"], True)
        self.assertEqual(written["model_display"], "Gemini 3 Flash (preview)")


class CapabilitiesTests(LLMTestCase):
    def test_signed_in_users_only(self):
        self.assertIn(APIClient().get(CAPABILITIES).status_code, (401, 403))

    def test_without_a_generative_model_chat_quotes(self):
        response = self.client_for(make_user("reader")).get(CAPABILITIES)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "generative_available": False,
                "mode": "quoting",
                "resumes_at": None,
                "general_knowledge": False,
            },
        )
        self.assertIn("max-age", response["Cache-Control"])
        # A read of service state, not of anyone's data.
        self.assertFalse(AuditLog.objects.exists())

    def test_writing_and_when_it_resumes(self):
        client = ModelClient()
        answerer = groq_answerer(client)
        with mock.patch.object(engine, "get_answerer", return_value=answerer):
            data = self.client_for(make_user("writer")).get(CAPABILITIES).data
            self.assertEqual(data["mode"], "writing")
            self.assertTrue(data["generative_available"])

            client.failing = {"openai/gpt-oss-120b": 429}
            with self.assertRaises(Exception):
                answerer.gateway.complete(
                    [{"role": "user", "content": "q"}], purpose="chat", max_tokens=8
                )
            ChatCapabilitiesView._cached = (0.0, None)
            data = self.client_for(make_user("waiter")).get(CAPABILITIES).data
        self.assertEqual(data["mode"], "quoting")
        self.assertIsNotNone(data["resumes_at"])


class SystemStatusAITests(LLMTestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user("ai_root", Role.SYSTEM_ADMIN, "")
        self.client = self.client_for(self.root)
        self.answerer = groq_answerer(ModelClient(text="OK"))
        patch = mock.patch.object(engine, "get_answerer", return_value=self.answerer)
        patch.start()
        self.addCleanup(patch.stop)

    def test_system_status_reports_the_chain_without_secrets(self):
        with (
            mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-do-not-show"}),
            mock.patch.dict(
                health.CHECKS, {"broker": lambda: None, "storage": lambda: None}
            ),
            mock.patch(
                "documents.operations.health.queues.collect", return_value={"ingest": 0}
            ),
        ):
            response = self.client.get(reverse("admin-system"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        ai = response.data["ai"]
        # What was there before is still there.
        self.assertEqual(ai["answerer"]["name"], "openai-compatible")
        [row] = ai["chain"]
        for key in (
            "provider",
            "model",
            "state",
            "cooling_until",
            "calls_today",
            "daily_limit",
            "last_error_type",
            "last_success_at",
            "trains_on_input",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["state"], "ready")
        self.assertEqual(
            set(ai["totals"]["answers_today"]),
            {"generative", "quoted", "abstained", "quoted_ai_unavailable", "since"},
        )
        body = json.dumps(response.data, default=str)
        self.assertNotIn("gsk-do-not-show", body)
        self.assertNotIn("api.groq.com", body)

    def test_the_panel_endpoint_is_for_system_administrators(self):
        department_admin = make_user("ai_dept", Role.DEPARTMENT_ADMIN, "Physics")
        for path in (reverse("admin-ai"), reverse("admin-ai-test")):
            client = self.client_for(department_admin)
            response = (
                client.get(path)
                if path.endswith("/ai/")
                else client.post(path, {"link": "groq:openai/gpt-oss-120b"})
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, path)
        self.assertEqual(self.client.get(reverse("admin-ai")).status_code, 200)

    def test_a_probe_reports_the_link_and_is_audited(self):
        response = self.client.post(
            reverse("admin-ai-test"),
            {"link": "groq:openai/gpt-oss-120b"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(response.data["ok"])
        self.assertEqual(response.data["state"]["calls_today"], 1)
        request = self.answerer._client.calls[0]
        self.assertEqual(request["max_tokens"], 1)
        self.assertNotIn(PASSAGE[:20], json.dumps(request))

        row = AuditLog.objects.get(action=AuditLog.Action.AI_PROVIDER_TEST)
        self.assertEqual(row.user, self.root)
        self.assertEqual(row.metadata["link"], "groq:openai/gpt-oss-120b")
        self.assertTrue(row.metadata["ok"])

    def test_a_failed_probe_says_how_and_benches_the_link(self):
        self.answerer._client.failing = {"openai/gpt-oss-120b": 429}
        data = self.client.post(
            reverse("admin-ai-test"), {"link": "groq:openai/gpt-oss-120b"}
        ).data
        self.assertFalse(data["ok"])
        self.assertEqual(
            (data["error_type"], data["status_code"]), ("StatusError", 429)
        )
        self.assertEqual(data["state"]["state"], "cooling")
        self.assertFalse(
            AuditLog.objects.get(action=AuditLog.Action.AI_PROVIDER_TEST).metadata["ok"]
        )

    def test_an_unknown_link_is_not_found_and_an_unconfigured_one_is_not_sent(self):
        self.assertEqual(
            self.client.post(
                reverse("admin-ai-test"), {"link": "groq:other"}
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        with override_settings(RAG_ANSWERER_PROVIDER="gemini", RAG_ANSWERER_MODEL=""):
            from documents.rag.openai_compat import OpenAICompatibleAnswerer

            keyless = OpenAICompatibleAnswerer()
        with mock.patch.object(engine, "get_answerer", return_value=keyless):
            data = self.client.post(
                reverse("admin-ai-test"), {"link": "gemini:gemini-3.5-flash"}
            ).data
        self.assertEqual((data["ok"], data["error_type"]), (False, "unconfigured"))
        self.assertEqual(data["state"]["state"], "unconfigured")

    def test_probes_are_tightly_throttled(self):
        codes = [
            self.client.post(
                reverse("admin-ai-test"), {"link": "groq:openai/gpt-oss-120b"}
            ).status_code
            for _ in range(11)
        ]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], status.HTTP_429_TOO_MANY_REQUESTS)

    def test_answers_today_count_how_answers_were_produced(self):
        conversation = Conversation.objects.create(owner=self.root)
        for generative, abstained, degraded in (
            (True, False, False),
            (False, False, True),
            (False, True, False),
        ):
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content="x",
                generative=generative,
                abstained=abstained,
                metadata={"degraded": {"answerer": degraded}},
            )
        counts = self.client.get(reverse("admin-ai")).data["totals"]["answers_today"]
        self.assertEqual(
            (
                counts["generative"],
                counts["quoted"],
                counts["abstained"],
                counts["quoted_ai_unavailable"],
            ),
            (1, 1, 1, 1),
        )
