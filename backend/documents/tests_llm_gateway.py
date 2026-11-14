"""The LLM gateway: the chain walk, the shared budget, purposes and labels.

Driven with stub clients wired per endpoint, the way tests_providers drives a
single answerer: what is under test is where a request goes, what is counted
and what is skipped - never a live provider.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from documents.rag import budget, gateway, labels
from documents.rag.gateway import (
    BRIEF,
    CHAT,
    GRID,
    Gateway,
    OpenAICompatibleNotConfigured,
    Unavailable,
)
from documents.rag.providers import PRESETS
from documents.tests_providers import StubChatClient, completion

# Every preset's key variable, blanked: a developer's exported key must not
# make a link configured behind a test's back.
NO_KEYS = {preset.key_env: "" for preset in PRESETS.values()}
NOON = datetime(2026, 11, 7, 12, 0, tzinfo=UTC).timestamp()


class StatusError(Exception):
    def __init__(self, status_code, message=""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


def chunk(text=None, finish=None):
    return SimpleNamespace(
        model="stub",
        usage=None,
        choices=[
            SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)
        ],
    )


class ModelClient(StubChatClient):
    """Fails the models in `failing` (a status code or an exception);
    answers the rest, streamed when asked."""

    def __init__(self, failing=None, text="Answer [1]."):
        super().__init__(completion=completion(text))
        self.failing = failing or {}
        self.text = text

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        failure = self.failing.get(kwargs["model"])
        if isinstance(failure, Exception):
            raise failure
        if failure:
            raise StatusError(failure)
        if kwargs.get("stream"):
            return iter([chunk(self.text), chunk(None, "stop")])
        return self.completion


class BrokenStream(ModelClient):
    """Streams one piece, then loses the connection."""

    def _create(self, **kwargs):
        self.calls.append(kwargs)

        def broken():
            yield chunk("Half an ans")
            raise ConnectionError("reset")

        return broken()


def build(chain="gemini,groq", **overrides):
    settings = {"RAG_ANSWERER_CHAIN": chain, "LLM_DAILY_LIMITS": "", **overrides}
    with override_settings(**settings):
        return Gateway.from_settings()


def wire(gateway, provider, client):
    """Give every link of `provider` this client, as if its key were set."""
    for link in gateway.links:
        if link.provider == provider:
            gateway._clients[link.endpoint] = client
    return client


def ask(gateway, purpose=CHAT, **kwargs):
    return gateway.complete(
        [{"role": "user", "content": "q"}], purpose=purpose, max_tokens=64, **kwargs
    )


class GatewayTestCase(SimpleTestCase):
    def setUp(self):
        cache.clear()
        budget.reset()
        patch = mock.patch.dict(os.environ, NO_KEYS)
        patch.start()
        self.addCleanup(patch.stop)


class ChainConfigurationTests(GatewayTestCase):
    def test_a_bare_provider_is_its_preset_model_list(self):
        chain = build("gemini, groq:openai/gpt-oss-120b, ollama:gpt-oss:20b")
        self.assertEqual(
            [link.id for link in chain.links],
            [
                "gemini:gemini-3.5-flash",
                "gemini:gemini-3-flash-preview",
                "gemini:gemini-3.7-flash",
                "gemini:gemini-3.8-flash",
                "groq:openai/gpt-oss-120b",
                # Split at the first colon only: the size is part of the id.
                "ollama:gpt-oss:20b",
            ],
        )

    def test_without_a_chain_the_single_provider_is_unchanged(self):
        """The live configuration: RAG_ANSWERER_PROVIDER=gemini and a key."""
        chain = build("", RAG_ANSWERER_PROVIDER="gemini", RAG_ANSWERER_MODEL="")
        self.assertEqual(
            [link.model for link in chain.links],
            [PRESETS["gemini"].model, *PRESETS["gemini"].fallback_models],
        )
        self.assertEqual(chain.links[0].daily_limit, 20)
        self.assertEqual(chain.links[0].quota_tz, "America/Los_Angeles")

    def test_an_unknown_provider_in_the_chain_is_refused(self):
        with self.assertRaises(OpenAICompatibleNotConfigured):
            build("gemini, grok")

    def test_custom_needs_a_base_url(self):
        with self.assertRaises(OpenAICompatibleNotConfigured):
            build("custom:llama", RAG_ANSWERER_BASE_URL="")
        chain = build("custom:llama", RAG_ANSWERER_BASE_URL="https://llm.internal/v1")
        self.assertEqual(chain.links[0].key_env, "RAG_ANSWERER_API_KEY")

    def test_a_reasoning_override_only_touches_links_that_send_it(self):
        chain = build(
            "gemini:gemini-3.5-flash,mistral", RAG_ANSWERER_REASONING_EFFORT="low"
        )
        extras = {link.provider: dict(link.request_extras) for link in chain.links}
        self.assertEqual(extras["gemini"], {"reasoning_effort": "low"})
        self.assertEqual(extras["mistral"], {})

    def test_daily_limits_most_specific_key_wins(self):
        chain = build(
            "gemini,groq",
            LLM_DAILY_LIMITS="*=7,gemini=30,gemini:gemini-3.7-flash=5",
        )
        limits = {link.id: link.daily_limit for link in chain.links}
        self.assertEqual(limits["gemini:gemini-3.5-flash"], 30)
        self.assertEqual(limits["gemini:gemini-3.7-flash"], 5)
        self.assertEqual(limits["groq:openai/gpt-oss-120b"], 7)

    def test_an_unknown_purpose_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            build().available("summaries")


class ChainWalkTests(GatewayTestCase):
    def test_a_rate_limited_provider_hands_over_to_the_next_one(self):
        chain = build()
        gemini = wire(
            chain,
            "gemini",
            ModelClient(
                dict.fromkeys(
                    [link.model for link in chain.links if link.provider == "gemini"],
                    429,
                )
            ),
        )
        groq = wire(chain, "groq", ModelClient(text="From Groq [1]."))

        response = ask(chain)

        self.assertEqual(response.text, "From Groq [1].")
        self.assertEqual(response.provider, "groq")
        self.assertEqual(response.model, "openai/gpt-oss-120b")
        self.assertEqual(response.model_version, "groq/openai/gpt-oss-120b")
        self.assertEqual(response.fell_back_from, "gemini/gemini-3.5-flash")
        self.assertEqual(len(gemini.calls), 4)
        # Each link gets its own provider's extras and nobody else's.
        self.assertEqual(gemini.calls[0]["reasoning_effort"], "none")
        self.assertEqual(groq.calls[0]["reasoning_effort"], "low")
        # And its own ceiling on the reply it asks for (Groq's 8K a minute).
        groq_link = next(link for link in chain.links if link.provider == "groq")
        self.assertEqual(groq_link.token_cap(4096), 1500)
        self.assertEqual(groq_link.token_cap(200), 200)

    def test_a_refused_key_skips_that_provider_not_the_chain(self):
        chain = build()
        gemini = wire(chain, "gemini", ModelClient({"gemini-3.5-flash": 401}))
        wire(chain, "groq", ModelClient())

        ask(chain)

        # One call to the refused account, none to its other models.
        self.assertEqual([call["model"] for call in gemini.calls], ["gemini-3.5-flash"])

    def test_a_request_one_model_rejects_moves_to_the_next_model(self):
        chain = build("gemini")
        client = wire(
            chain,
            "gemini",
            ModelClient({"gemini-3.5-flash": 400, "gemini-3-flash-preview": 404}),
        )
        response = ask(chain)
        self.assertEqual(response.model, "gemini-3.7-flash")
        self.assertEqual(len(client.calls), 3)

    def test_an_unreachable_host_skips_its_links_and_tries_the_next_provider(self):
        chain = build()
        gemini = wire(
            chain,
            "gemini",
            ModelClient({"gemini-3.5-flash": TimeoutError("read timed out")}),
        )
        wire(chain, "groq", ModelClient())
        self.assertEqual(ask(chain).provider, "groq")
        self.assertEqual(len(gemini.calls), 1)

    def test_links_without_a_key_are_never_tried(self):
        chain = build()
        groq = wire(chain, "groq", ModelClient())
        response = ask(chain)
        self.assertEqual(response.provider, "groq")
        # Nothing fell back: a link with no key was never a choice.
        self.assertEqual(response.fell_back_from, "")
        self.assertEqual(len(groq.calls), 1)

    def test_nothing_configured_is_reported_as_not_configured(self):
        with self.assertRaises(OpenAICompatibleNotConfigured) as caught:
            ask(build())
        self.assertIsInstance(caught.exception, Unavailable)
        self.assertIn("GEMINI_API_KEY", str(caught.exception))

    def test_every_link_failing_is_unavailable_with_its_cause(self):
        chain = build("groq")
        wire(chain, "groq", ModelClient({"openai/gpt-oss-120b": 500}))
        with self.assertRaises(Unavailable) as caught:
            ask(chain)
        self.assertEqual(caught.exception.reason, gateway.FAILED)
        self.assertIsInstance(caught.exception.__cause__, StatusError)

    def test_json_mode_is_asked_for_only_where_it_is_known_to_work(self):
        chain = build("gemini:gemini-3.5-flash,groq")
        gemini = wire(chain, "gemini", ModelClient({"gemini-3.5-flash": 503}))
        groq = wire(chain, "groq", ModelClient(text="{}"))
        ask(chain, json_mode=True)
        self.assertNotIn("response_format", gemini.calls[0])
        self.assertEqual(groq.calls[0]["response_format"], {"type": "json_object"})

    def test_an_output_ceiling_is_respected_per_link(self):
        chain = build("github")
        client = wire(chain, "github", ModelClient())
        chain.complete(
            [{"role": "user", "content": "q"}], purpose=CHAT, max_tokens=8000
        )
        self.assertEqual(client.calls[0]["max_tokens"], 4000)


class StreamingAcrossLinksTests(GatewayTestCase):
    def test_a_broken_stream_is_withdrawn_before_the_next_link_speaks(self):
        chain = build()
        wire(chain, "gemini", BrokenStream())
        wire(chain, "groq", ModelClient(text="Whole answer [1]."))
        events = []

        response = ask(
            chain,
            on_delta=lambda text: events.append(("delta", text)),
            on_reset=lambda: events.append(("reset", "")),
        )

        self.assertEqual(
            events,
            [("delta", "Half an ans"), ("reset", ""), ("delta", "Whole answer [1].")],
        )
        self.assertEqual(response.text, "Whole answer [1].")

    def test_without_a_way_to_withdraw_it_a_broken_stream_ends_the_walk(self):
        chain = build()
        wire(chain, "gemini", BrokenStream())
        groq = wire(chain, "groq", ModelClient())
        with self.assertRaises(Unavailable):
            ask(chain, on_delta=lambda text: None)
        self.assertEqual(groq.calls, [])


class SharedBudgetTests(GatewayTestCase):
    def test_a_cooldown_one_process_learned_is_known_to_another(self):
        """Two gateways built separately - two gunicorn workers - share it."""
        first, second = build("gemini"), build("gemini")
        wire(first, "gemini", ModelClient({"gemini-3.5-flash": 429}))
        client = wire(second, "gemini", ModelClient())

        ask(first)
        ask(second)

        self.assertEqual(client.calls[0]["model"], "gemini-3-flash-preview")
        row = second.status()["chain"][0]
        self.assertEqual(row["state"], gateway.COOLING)
        self.assertIsNotNone(row["cooling_until"])
        self.assertEqual(row["last_error_type"], "StatusError")
        self.assertEqual(row["last_error_status"], 429)

    def test_calls_are_counted_once_across_processes(self):
        first, second = build("groq"), build("groq")
        wire(first, "groq", ModelClient())
        wire(second, "groq", ModelClient())
        ask(first)
        ask(second)
        self.assertEqual(first.status()["chain"][0]["calls_today"], 2)

    def test_a_rate_limited_request_is_not_counted(self):
        chain = build("gemini:gemini-3.5-flash,groq")
        wire(chain, "gemini", ModelClient({"gemini-3.5-flash": 429}))
        wire(chain, "groq", ModelClient())
        ask(chain)
        rows = {row["provider"]: row for row in chain.status()["chain"]}
        self.assertEqual(rows["gemini"]["calls_today"], 0)
        self.assertEqual(rows["groq"]["calls_today"], 1)

    def test_a_request_too_large_for_a_free_tier_is_benched_not_retried(self):
        """Groq answers 413 when the prompt does not fit its tokens-per-minute
        window. It was not benched, so every question first paid a wasted round
        trip to the same refusal; and it was counted against the day."""
        chain = build("groq,cerebras")
        groq = wire(chain, "groq", ModelClient({"openai/gpt-oss-120b": 413}))
        cerebras = wire(chain, "cerebras", ModelClient())

        ask(chain)
        ask(chain)

        # The first question tried Groq and moved on; the second never did.
        self.assertEqual(len(groq.calls), 1)
        self.assertEqual(len(cerebras.calls), 2)
        rows = {row["provider"]: row for row in chain.status()["chain"]}
        self.assertEqual(rows["groq"]["calls_today"], 0)
        self.assertEqual(rows["groq"]["state"], gateway.COOLING)
        self.assertEqual(rows["groq"]["last_error_status"], 413)

    def test_chat_tries_a_cooling_link_last_and_other_purposes_wait(self):
        chain = build("gemini:gemini-3.5-flash,groq")
        wire(chain, "gemini", ModelClient({"gemini-3.5-flash": 429}))
        groq = wire(chain, "groq", ModelClient({"openai/gpt-oss-120b": 429}))
        with self.assertRaises(Unavailable):
            ask(chain)  # both benched now

        self.assertEqual(
            [link.id for link in chain.plan(CHAT).links],
            ["gemini:gemini-3.5-flash", "groq:openai/gpt-oss-120b"],
        )
        plan = chain.plan(BRIEF)
        self.assertEqual(plan.links, [])
        self.assertEqual(plan.unavailable.reason, gateway.ALL_COOLING)
        self.assertIsNotNone(plan.unavailable.retry_at)
        self.assertFalse(chain.available(CHAT))
        self.assertEqual(len(groq.calls), 1)

    def test_a_link_that_used_its_day_is_skipped_until_its_quota_midnight(self):
        with mock.patch.object(budget, "_now", return_value=NOON):
            chain = build("gemini:gemini-3.5-flash,groq", LLM_DAILY_LIMITS="gemini=2")
            gemini = wire(chain, "gemini", ModelClient())
            groq = wire(chain, "groq", ModelClient())
            for _ in range(3):
                ask(chain)
            self.assertEqual(len(gemini.calls), 2)
            self.assertEqual(len(groq.calls), 1)
            row = chain.status()["chain"][0]
            self.assertEqual(row["state"], gateway.EXHAUSTED)
            # Gemini's day ends at midnight Pacific: 08:00 UTC in November.
            self.assertEqual(row["resets_at"], "2026-11-08T08:00:00+00:00")

        # The next Pacific day starts a fresh count.
        tomorrow = datetime(2026, 11, 8, 8, 1, tzinfo=UTC).timestamp()
        with mock.patch.object(budget, "_now", return_value=tomorrow):
            ask(chain)
        self.assertEqual(len(gemini.calls), 3)

    def test_when_every_link_used_its_day_chat_is_told_when_it_comes_back(self):
        with mock.patch.object(budget, "_now", return_value=NOON):
            chain = build("groq", LLM_DAILY_LIMITS="groq=1")
            wire(chain, "groq", ModelClient())
            ask(chain)
            with self.assertRaises(Unavailable) as caught:
                ask(chain)
            self.assertEqual(caught.exception.reason, gateway.NO_CAPACITY)
            self.assertEqual(
                caught.exception.retry_at, datetime(2026, 11, 8, tzinfo=UTC)
            )
            self.assertTrue(gateway.is_capacity(caught.exception))
            self.assertEqual(chain.resumes_at(), datetime(2026, 11, 8, tzinfo=UTC))

    def test_a_per_day_refusal_marks_the_link_spent_whatever_the_count(self):
        daily = StatusError(
            429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        chain = build("gemini:gemini-3.5-flash")
        wire(chain, "gemini", ModelClient({"gemini-3.5-flash": daily}))
        with self.assertRaises(Unavailable):
            ask(chain)
        self.assertEqual(chain.status()["chain"][0]["state"], gateway.EXHAUSTED)
        self.assertEqual(chain.plan(CHAT).unavailable.reason, gateway.NO_CAPACITY)

    def test_an_explicit_probe_ignores_the_bench_and_clears_it_on_success(self):
        chain = build("gemini:gemini-3.5-flash")
        client = wire(chain, "gemini", ModelClient({"gemini-3.5-flash": 429}))
        with self.assertRaises(Unavailable):
            ask(chain)
        client.failing = {}
        chain.run(
            lambda link: {"model": link.model, "messages": []}, pin=chain.links[0]
        )
        self.assertEqual(chain.status()["chain"][0]["state"], gateway.READY)

    def test_the_bookkeeping_is_not_a_dependency(self):
        """With the cache down, answers flow on per-process state."""
        chain = build("groq")
        wire(chain, "groq", ModelClient())
        broken = mock.Mock(side_effect=ConnectionError("redis is down"))
        with (
            mock.patch.object(budget._CacheStore, "get_many", broken),
            mock.patch.object(budget._CacheStore, "incr", broken),
            mock.patch.object(budget._CacheStore, "set", broken),
            mock.patch.object(budget._CacheStore, "delete_many", broken),
            # Logged once a minute per process; another test may have just.
            mock.patch.object(budget.broker._outage, "last_logged", 0.0),
            self.assertLogs("adi.llm", level="WARNING") as logs,
        ):
            self.assertEqual(ask(chain).provider, "groq")
            self.assertEqual(chain.status()["chain"][0]["calls_today"], 1)
        self.assertIn(
            "llm.budget_cache_unavailable", [r.getMessage() for r in logs.records]
        )


@override_settings(LLM_CHAT_RESERVE=2)
class ChatReserveTests(GatewayTestCase):
    def chain(self):
        chain = build("groq", LLM_DAILY_LIMITS="groq=6")
        self.client = wire(chain, "groq", ModelClient())
        return chain

    def test_briefs_stop_while_the_reserve_is_left_for_chat(self):
        chain = self.chain()
        for _ in range(4):  # 6 - 4 = 2 left: the reserve
            ask(chain, BRIEF)
        with self.assertRaises(Unavailable) as caught:
            ask(chain, BRIEF)
        self.assertEqual(caught.exception.reason, gateway.RESERVED)
        self.assertFalse(chain.available(BRIEF))
        # Chat may spend the reserve.
        self.assertTrue(chain.available(CHAT))
        ask(chain, CHAT)
        ask(chain, CHAT)
        self.assertEqual(len(self.client.calls), 6)

    def test_lower_priorities_keep_more_back(self):
        chain = self.chain()
        ask(chain, BRIEF)
        ask(chain, BRIEF)  # 4 left: a grid must leave twice the reserve
        self.assertFalse(chain.available(GRID))
        self.assertTrue(chain.available(BRIEF))

    def test_an_unlimited_link_serves_every_purpose(self):
        chain = build("ollama", LLM_DAILY_LIMITS="")
        wire(chain, "ollama", ModelClient())
        self.assertTrue(chain.available(gateway.OTHER))
        self.assertIsNone(chain.status()["totals"]["capacity_left_today"])


class StatusTests(GatewayTestCase):
    def test_status_names_links_and_variables_but_never_keys_or_urls(self):
        chain = build("gemini:gemini-3.5-flash,groq,openrouter")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk-secret-value"}):
            status = chain.status()
        body = repr(status)
        self.assertNotIn("gsk-secret-value", body)
        self.assertNotIn("https://", body)
        rows = {row["provider"]: row for row in status["chain"]}
        self.assertEqual(rows["gemini"]["state"], gateway.UNCONFIGURED)
        self.assertEqual(rows["groq"]["state"], gateway.READY)
        self.assertEqual(rows["groq"]["key_env"], "GROQ_API_KEY")
        self.assertTrue(rows["openrouter"]["trains_on_input"])
        self.assertEqual(rows["gemini"]["label"], "Gemini 3.5 Flash")
        # Only configured links count towards what is left today.
        self.assertEqual(
            status["totals"]["capacity_left_today"], PRESETS["groq"].daily_limit
        )

    def test_no_generation_configured_is_an_empty_disabled_status(self):
        status = gateway.status()  # the suite's extractive answerer
        self.assertFalse(status["enabled"])
        self.assertEqual(status["chain"], [])
        self.assertFalse(gateway.available(CHAT))
        with self.assertRaises(Unavailable):
            gateway.complete([], purpose=BRIEF, max_tokens=10)


class ModelLabelTests(SimpleTestCase):
    def test_names_read_the_way_their_publishers_write_them(self):
        for provider, model, served_by, expected in (
            ("gemini", "gemini-3.5-flash", "", "Gemini 3.5 Flash"),
            ("gemini", "gemini-3-flash-preview", "", "Gemini 3 Flash (preview)"),
            ("groq", "openai/gpt-oss-120b", "", "GPT-OSS 120B · Groq"),
            ("cerebras", "gpt-oss-120b", "", "GPT-OSS 120B · Cerebras"),
            ("github", "openai/gpt-4.1-mini", "", "GPT-4.1 mini · GitHub Models"),
            ("mistral", "mistral-small-latest", "", "Mistral Small"),
            ("openai", "gpt-4.1-mini", "", "GPT-4.1 mini"),
            ("ollama", "gpt-oss:20b", "", "GPT-OSS 20B · Ollama (local)"),
            (
                "openrouter",
                "openrouter/free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "Llama 3.3 70B Instruct · OpenRouter",
            ),
            ("openrouter", "openrouter/free", "", "Free model · OpenRouter"),
            ("custom", "llama", "", "Llama · custom endpoint"),
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    labels.model_display(provider, model, served_by=served_by),
                    expected,
                )

    def test_stored_versions_and_quoted_answers(self):
        self.assertEqual(
            labels.display_for_version("groq/openai/gpt-oss-120b", generative=True),
            "GPT-OSS 120B · Groq",
        )
        self.assertEqual(
            labels.display_for_version("claude-opus-5", generative=True),
            "Claude Opus 5",
        )
        self.assertEqual(
            labels.display_for_version("0.1.0", generative=False), labels.QUOTED
        )
