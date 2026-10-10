"""Contract tests for the optional AI providers.

Every provider here talks to something this suite must not depend on - a
hosted API, an AWS account, or a model download - so each one is driven
with a stub standing in for exactly the object it calls. What is under test
is the provider's side of the contract: the request it builds, how it reads
the response, and above all what it does when the other side fails.

That last part is the one worth the most. Every provider promises to
degrade rather than 500, and a degraded answer that does not SAY it was
degraded is worse than a slow one. So each failure test asserts both that
the call survived and that the degradation is visible.

NONE of this proves a live backend works. Real verification of the hosted
providers needs credentials this repository does not have; the local
models are verified for real by the evaluation runs documented in
docs/evaluation/, not here.
"""

import io
import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from documents import rag
from documents.processing.embedding.bedrock import (
    BedrockEmbeddingUnavailable,
    BedrockTitanEmbeddingProvider,
)
from documents.processing.embedding.local import (
    LocalEmbeddingProvider,
    LocalEmbeddingUnavailable,
)
from documents.rag import engine
from documents.rag.bedrock_rerank import BedrockReranker
from documents.rag.claude import SYSTEM
from documents.rag.llm import ABSTENTION_TEXT
from documents.rag.local_rerank import LocalCrossEncoderReranker
from documents.rag.openai_compat import (
    PRESETS,
    OpenAICompatibleAnswerer,
    OpenAICompatibleNotConfigured,
    _without_credentials,
)
from documents.tests_rag import build_context, chunk, hits

PASSAGE = (
    "A student must maintain a minimum of 75 percent attendance in each "
    "course to be eligible for the end-semester examination."
)


# --- OpenAI-compatible answerer ---------------------------------------------


@dataclass
class StubChoice:
    content: str
    finish_reason: str = "stop"

    @property
    def message(self):
        return SimpleNamespace(content=self.content)


@dataclass
class StubCompletion:
    choices: list
    model: str = "stub-model"
    usage: object = field(
        default_factory=lambda: SimpleNamespace(prompt_tokens=120, completion_tokens=18)
    )
    model_extra: dict = field(default_factory=dict)


class StubChatClient:
    """Shaped like `openai.OpenAI`: client.chat.completions.create(...)."""

    def __init__(self, completion=None, raises=None):
        self.completion = completion
        self.raises = raises
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.completion


def answerer_with(client, **settings_overrides):
    with override_settings(**settings_overrides):
        answerer = OpenAICompatibleAnswerer()
    answerer._client = client
    return answerer


def completion(text, finish_reason="stop"):
    return StubCompletion(
        choices=[StubChoice(content=text, finish_reason=finish_reason)]
    )


@override_settings(
    RAG_ANSWERER_PROVIDER="groq",
    RAG_ANSWERER_BASE_URL="",
    RAG_ANSWERER_MODEL="",
)
class OpenAICompatibleConfigurationTests(SimpleTestCase):
    def test_a_preset_is_the_whole_configuration(self):
        answerer = OpenAICompatibleAnswerer()
        preset = PRESETS["groq"]

        self.assertEqual(answerer.base_url, preset.base_url)
        self.assertEqual(answerer.model, preset.model)
        self.assertEqual(answerer.key_env, preset.key_env)
        self.assertEqual(answerer.version, f"groq/{preset.model}")

    @override_settings(RAG_ANSWERER_PROVIDER="not-a-provider")
    def test_an_unknown_provider_without_a_url_is_refused(self):
        with self.assertRaises(OpenAICompatibleNotConfigured):
            OpenAICompatibleAnswerer()

    @override_settings(
        RAG_ANSWERER_PROVIDER="internal",
        RAG_ANSWERER_BASE_URL="https://llm.internal.example/v1",
    )
    def test_a_custom_endpoint_must_name_its_model(self):
        """There is no default model for a server this code has never seen."""
        with self.assertRaises(OpenAICompatibleNotConfigured):
            OpenAICompatibleAnswerer()

    @override_settings(
        RAG_ANSWERER_PROVIDER="internal",
        RAG_ANSWERER_BASE_URL="https://llm.internal.example/v1",
        RAG_ANSWERER_MODEL="served-model",
    )
    def test_explicit_settings_reach_an_unlisted_server(self):
        answerer = OpenAICompatibleAnswerer()
        self.assertEqual(answerer.base_url, "https://llm.internal.example/v1")
        self.assertEqual(answerer.model, "served-model")

    @override_settings(
        RAG_ANSWERER_PROVIDER="",
        RAG_ANSWERER_BASE_URL="https://gateway.internal/v1",
        RAG_ANSWERER_MODEL="llama",
    )
    def test_a_base_url_without_a_provider_inherits_no_preset(self):
        """With the provider name defaulted to Gemini, a custom endpoint got
        Gemini's key variable - an exported GEMINI_API_KEY was sent to it as
        the bearer token - and Gemini's training flag and version label."""
        answerer = OpenAICompatibleAnswerer()

        self.assertEqual(answerer.provider, "custom")
        self.assertEqual(answerer.key_env, "RAG_ANSWERER_API_KEY")
        self.assertFalse(answerer.trains_on_free_tier)
        self.assertEqual(answerer.version, "custom/llama")
        with (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "google-key"}, clear=True),
            self.assertRaises(OpenAICompatibleNotConfigured),
        ):
            answerer._api_key()

    @override_settings(
        RAG_ANSWERER_PROVIDER="",
        RAG_ANSWERER_BASE_URL="https://gateway.internal/v1",
        RAG_ANSWERER_MODEL="llama",
        RAG_ANSWERER_KEY_ENV="GATEWAY_TOKEN",
        RAG_ANSWERER_PROVIDER_TRAINS_ON_INPUT=True,
    )
    def test_the_documented_overrides_take_effect(self):
        answerer = OpenAICompatibleAnswerer()

        self.assertEqual(answerer.key_env, "GATEWAY_TOKEN")
        self.assertTrue(answerer.trains_on_free_tier)

    def test_the_key_is_read_from_the_environment_only(self):
        answerer = OpenAICompatibleAnswerer()
        with mock.patch.dict(os.environ, {answerer.key_env: "secret-from-env"}):
            self.assertEqual(answerer._api_key(), "secret-from-env")

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAICompatibleNotConfigured):
                answerer._api_key()

    @override_settings(RAG_ANSWERER_PROVIDER="ollama")
    def test_ollama_needs_no_real_key(self):
        """The SDK refuses to build a client without one; Ollama ignores it."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(OpenAICompatibleAnswerer()._api_key(), "ollama")

    @override_settings(
        RAG_ANSWERER_PROVIDER="gemini",
        RAG_ANSWERER_BASE_URL=(
            "https://svc:s3cret@egress.internal"
            "/generativelanguage.googleapis.com/v1beta/openai"
        ),
    )
    def test_the_suspect_url_warning_carries_no_credentials(self):
        """The missing trailing slash is the typo this warning exists for,
        and it logged the URL whole - password included, since the formatter
        redacts by field name and `base_url` is not one it knows."""
        with self.assertLogs("adi.llm", level="WARNING") as logs:
            OpenAICompatibleAnswerer()

        [record] = [r for r in logs.records if r.getMessage() == "llm.suspect_base_url"]
        self.assertNotIn("s3cret", record.base_url)
        self.assertIn("egress.internal", record.base_url)

    def test_credentials_are_stripped_from_a_stored_url(self):
        self.assertEqual(
            _without_credentials("https://user:pw@gateway.internal/v1"),
            "https://gateway.internal/v1",
        )
        self.assertEqual(
            _without_credentials("https://api.groq.com/openai/v1"),
            "https://api.groq.com/openai/v1",
        )


@override_settings(
    RAG_ANSWERER_PROVIDER="groq",
    RAG_ANSWERER_BASE_URL="",
    RAG_ANSWERER_MODEL="",
)
class OpenAICompatibleAnsweringTests(SimpleTestCase):
    def setUp(self):
        self.context = build_context(PASSAGE)

    def ask(self, answerer):
        return answerer.answer(
            question="What is the minimum attendance?",
            context=self.context,
            prompt="PROMPT",
        )

    def test_the_request_carries_the_shared_system_prompt(self):
        """One copy of the rules, shared with the checker's other half."""
        client = StubChatClient(completion("The minimum is 75 percent [1]."))
        self.ask(answerer_with(client))

        request = client.calls[0]
        self.assertEqual(request["messages"][0], {"role": "system", "content": SYSTEM})
        self.assertEqual(request["messages"][1], {"role": "user", "content": "PROMPT"})
        self.assertEqual(request["temperature"], 0.0)

    def test_a_successful_answer_reports_what_produced_it(self):
        client = StubChatClient(completion("The minimum is 75 percent [1]."))
        response = self.ask(answerer_with(client))

        self.assertTrue(response.generative)
        self.assertFalse(response.abstained)
        self.assertEqual(response.cited_markers, (1,))
        self.assertEqual(response.prompt_tokens, 120)
        self.assertEqual(response.completion_tokens, 18)
        self.assertEqual(response.metadata["provider"], "groq")

    def test_the_abstention_sentence_is_an_abstention(self):
        response = self.ask(answerer_with(StubChatClient(completion(ABSTENTION_TEXT))))
        self.assertTrue(response.abstained)

    def test_a_content_filter_stop_is_an_abstention_not_an_answer(self):
        response = self.ask(
            answerer_with(
                StubChatClient(completion("", finish_reason="content_filter"))
            )
        )
        self.assertTrue(response.abstained)
        self.assertEqual(response.text, ABSTENTION_TEXT)
        # Without this the test passed with the content_filter branch gone:
        # its empty content abstains on the "no text" path all the same.
        self.assertIn("declined", response.metadata["reason"])

    def test_an_empty_completion_is_an_abstention(self):
        response = self.ask(answerer_with(StubChatClient(completion("   "))))
        self.assertTrue(response.abstained)

    def test_a_truncated_answer_is_an_abstention_not_an_answer(self):
        """Cut at max_tokens after its first cited sentence, this passes
        grounding - the marker resolves and 75 is in the passage - while
        stating, without its condition, something the passage does not."""
        cut = (
            "You need at least 75 percent attendance to sit the examination [1]. "
            "If you fall short, you may still sit the examination"
        )
        response = self.ask(
            answerer_with(StubChatClient(completion(cut, finish_reason="length")))
        )

        self.assertTrue(response.abstained)
        self.assertEqual(response.text, ABSTENTION_TEXT)
        self.assertIn("cut off", response.metadata["reason"])

    def test_a_cap_spent_on_reasoning_is_reported_as_truncation(self):
        """A reasoning model's thinking counts toward max_tokens; one that
        spent all of it returns no content. The truncation warning was
        skipped there and the abstention blamed the model for no text."""
        client = StubChatClient(completion(None, finish_reason="length"))

        with self.assertLogs("adi.llm", level="WARNING") as logs:
            response = self.ask(answerer_with(client))

        self.assertTrue(response.abstained)
        self.assertIn("cut off", response.metadata["reason"])
        self.assertIn("llm.truncated", [record.getMessage() for record in logs.records])

    def test_tokens_spent_on_an_unusable_completion_are_still_reported(self):
        """The engine records usage for every response so cost is not
        counted only on success; an abstention reporting zero undid that."""
        for finish_reason, text in (("content_filter", ""), ("length", None)):
            with self.subTest(finish_reason=finish_reason):
                response = self.ask(
                    answerer_with(
                        StubChatClient(completion(text, finish_reason=finish_reason))
                    )
                )
                self.assertTrue(response.abstained)
                self.assertEqual(response.prompt_tokens, 120)
                self.assertEqual(response.completion_tokens, 18)

    def test_a_200_with_no_choices_falls_back_and_says_so(self):
        """OpenRouter's error-wearing-a-success. Must not IndexError."""
        client = StubChatClient(
            StubCompletion(choices=[], model_extra={"error": {"code": 502}})
        )
        response = self.ask(answerer_with(client))

        self.assertFalse(response.generative)
        self.assertEqual(response.metadata["fell_back_from"], "openai-compatible")
        self.assertEqual(response.metadata["fallback_reason"], "NoChoicesReturned")

    def test_a_timeout_falls_back_and_says_so(self):
        client = StubChatClient(raises=TimeoutError("read timed out"))
        response = self.ask(answerer_with(client))

        self.assertFalse(response.generative)
        self.assertEqual(response.metadata["fallback_reason"], "TimeoutError")

    def test_a_missing_key_falls_back_rather_than_raising(self):
        with override_settings(RAG_ANSWERER_PROVIDER="groq"):
            answerer = OpenAICompatibleAnswerer()
        with mock.patch.dict(os.environ, {}, clear=True):
            response = self.ask(answerer)

        self.assertFalse(response.generative)
        self.assertEqual(
            response.metadata["fallback_reason"], "OpenAICompatibleNotConfigured"
        )


# --- Through the engine: the guardrails still apply --------------------------


class FakeSearchResponse:
    def __init__(self, found):
        self.hits = found
        self.semantic = False
        self.notes = []


class GuardrailsApplyToEveryAnswererTests(TestCase):
    """Swapping the answerer must not relax a single check."""

    def run_engine(self, answerer, reranker=None, text=PASSAGE):
        found = hits((chunk(1, text), 1.0))
        patches = [
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
        ]
        if reranker is not None:
            patches.append(
                mock.patch.object(engine, "get_reranker", return_value=reranker)
            )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return rag.ask(None, "What is the minimum attendance for the examination?")

    @override_settings(RAG_ANSWERER_PROVIDER="groq")
    def test_a_fabricated_figure_from_a_hosted_model_is_rejected(self):
        answerer = answerer_with(
            StubChatClient(completion("The minimum attendance is 80 percent [1]."))
        )
        answer = self.run_engine(answerer)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertTrue(
            any("80" in failure for failure in answer.grounding_failures),
            answer.grounding_failures,
        )

    @override_settings(RAG_ANSWERER_PROVIDER="groq")
    def test_a_citation_to_a_passage_that_does_not_exist_is_rejected(self):
        answerer = answerer_with(
            StubChatClient(completion("The minimum attendance is 75 percent [7]."))
        )
        answer = self.run_engine(answerer)

        self.assertTrue(answer.abstained)

    @override_settings(RAG_ANSWERER_PROVIDER="groq")
    def test_a_grounded_answer_is_returned_with_timings(self):
        answerer = answerer_with(
            StubChatClient(completion("You need 75 percent attendance [1]."))
        )
        answer = self.run_engine(answerer)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertTrue(answer.generative)
        for stage in ("retrieval", "rerank", "generation", "grounding"):
            self.assertIn(stage, answer.timings)

    @override_settings(RAG_ANSWERER_PROVIDER="groq")
    def test_a_provider_outage_is_named_in_the_answer(self):
        """The extractive fallback answers correctly - and must say it was a
        fallback, or the outage is invisible to everyone reading answers."""
        answerer = answerer_with(StubChatClient(raises=TimeoutError("timed out")))
        answer = self.run_engine(answerer)

        self.assertFalse(answer.generative)
        self.assertTrue(
            any("was unavailable" in note for note in answer.notes), answer.notes
        )

    @override_settings(RAG_ANSWERER_PROVIDER="groq")
    def test_a_reply_cut_off_at_the_token_limit_says_so(self):
        """MODEL_ABSTAINED reads "found nothing in the passages". A reply
        the provider cut off is a different fact, and the provider's reason
        reached only the log."""
        answerer = answerer_with(
            StubChatClient(completion("You need 75 percent", finish_reason="length"))
        )
        answer = self.run_engine(answerer)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)
        self.assertTrue(any("cut off" in note for note in answer.notes), answer.notes)

    @override_settings(RAG_RERANKER_LOCAL_MODEL="cross-encoder/ms-marco-MiniLM-L6-v2")
    def test_a_degraded_reranker_is_held_to_the_lexical_floor(self):
        """A model outage must not turn into a wave of false abstentions.

        MiniLM's calibrated floor is 0.30 (chosen here so the lexical 0.2
        below would FAIL it). When its model fails, the scores it returns
        are LEXICAL scores, which live on a different scale - holding them
        to 0.30 abstained on questions the lexical reranker would have
        answered at its own floor of 0.15.
        """
        reranker = LocalCrossEncoderReranker()
        reranker._model = FailingModel()

        with mock.patch("documents.rag.reranking.LexicalReranker.rerank") as lexical:

            def lexical_scores(query, candidates):
                from documents.rag.reranking import RerankedChunk

                return [
                    RerankedChunk(
                        chunk=candidate.chunk,
                        score=0.2,
                        retrieval_score=candidate.score,
                        signals={},
                    )
                    for candidate in candidates
                ]

            lexical.side_effect = lexical_scores
            answer = self.run_engine(engine.get_answerer(), reranker=reranker)

        self.assertNotEqual(
            answer.abstention_reason, engine.Abstention.NO_RELEVANT_PASSAGES
        )
        self.assertTrue(
            any("scored lexically" in note for note in answer.notes), answer.notes
        )

    def test_an_explicit_floor_still_wins_over_degradation(self):
        with override_settings(RAG_RELEVANCE_FLOOR=0.5):
            self.assertEqual(
                engine.resolve_relevance_floor(
                    LocalCrossEncoderReranker(), degraded=True
                ),
                0.5,
            )

    def test_an_undegraded_cross_encoder_keeps_its_calibrated_floor(self):
        """Per model: calibrated on the development set, confirmed once on
        the v2 test set. An uncalibrated model gets the default."""
        for model, floor in (
            ("", 0.02),  # the default model, bge-reranker-base
            ("BAAI/bge-reranker-base", 0.02),
            ("cross-encoder/ms-marco-MiniLM-L6-v2", 0.30),
            ("some/uncalibrated-reranker", 0.30),
        ):
            with (
                self.subTest(model=model),
                override_settings(
                    RAG_RELEVANCE_FLOOR=None, RAG_RERANKER_LOCAL_MODEL=model
                ),
            ):
                self.assertEqual(
                    engine.resolve_relevance_floor(LocalCrossEncoderReranker()), floor
                )


# --- Bedrock Titan embeddings ------------------------------------------------


class StubBedrockRuntime:
    def __init__(self, width=1024, raises=None):
        self.width = width
        self.raises = raises
        self.bodies = []

    def invoke_model(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        body = json.loads(kwargs["body"])
        self.bodies.append({"modelId": kwargs["modelId"], **body})
        return {
            "body": io.BytesIO(json.dumps({"embedding": [0.1] * self.width}).encode())
        }


# Titan Text Embeddings V2 emits 256, 512 or 1024 dimensions and nothing
# else, so the request contract is exercised at a width it supports. These
# stubs used to accept 384 - the column's width - and so pinned a request
# the real API rejects.
@override_settings(EMBEDDING_DIMENSIONS=1024)
class BedrockEmbeddingTests(SimpleTestCase):
    def provider(self, client):
        provider = BedrockTitanEmbeddingProvider()
        provider._client = client
        return provider

    def test_each_text_is_sent_at_the_columns_width_and_normalized(self):
        client = StubBedrockRuntime()
        batch = self.provider(client).embed(["first", "second"])

        self.assertEqual(len(batch.vectors), 2)
        self.assertTrue(batch.semantic)
        self.assertEqual(batch.dimensions, 1024)
        for body in client.bodies:
            self.assertEqual(body["dimensions"], 1024)
            self.assertTrue(body["normalize"])
            self.assertEqual(body["modelId"], "amazon.titan-embed-text-v2:0")

    def test_a_blank_text_costs_no_call(self):
        client = StubBedrockRuntime()
        batch = self.provider(client).embed(["", "   ", "real"])

        self.assertEqual(len(client.bodies), 1)
        self.assertEqual(batch.vectors[0], [0.0] * 1024)

    def test_order_is_preserved_across_concurrent_calls(self):
        class Echo(StubBedrockRuntime):
            def invoke_model(self, **kwargs):
                text = json.loads(kwargs["body"])["inputText"]
                value = float(text.split("-")[1])
                return {
                    "body": io.BytesIO(
                        json.dumps({"embedding": [value] * 1024}).encode()
                    )
                }

        texts = [f"text-{i}" for i in range(20)]
        batch = self.provider(Echo()).embed(texts)
        self.assertEqual([v[0] for v in batch.vectors], [float(i) for i in range(20)])

    def test_a_wrong_width_response_is_refused(self):
        """A vector of the wrong width cannot go in the column, and must not
        be padded or truncated into one."""
        with self.assertRaises(BedrockEmbeddingUnavailable):
            self.provider(StubBedrockRuntime(width=384)).embed(["text"])

    @override_settings(EMBEDDING_DIMENSIONS=384)
    def test_a_width_titan_cannot_produce_is_refused_at_load(self):
        """The column is 384 wide (bge-small); Titan cannot emit 384, and
        every chunk would have failed at the API instead."""
        with self.assertRaises(BedrockEmbeddingUnavailable) as raised:
            BedrockTitanEmbeddingProvider()
        self.assertIn("Migrate the column", str(raised.exception))

    @override_settings(AWS_BEDROCK_REGION="")
    def test_no_region_is_a_clear_error_not_a_silent_default(self):
        provider = BedrockTitanEmbeddingProvider()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(BedrockEmbeddingUnavailable):
                provider.embed(["text"])


# --- Bedrock rerank ----------------------------------------------------------


class StubAgentRuntime:
    def __init__(self, scores=None, raises=None):
        self.scores = scores or {}
        self.raises = raises
        self.requests = []

    def rerank(self, **kwargs):
        self.requests.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {
            "results": [
                {"index": index, "relevanceScore": score}
                for index, score in self.scores.items()
            ]
        }


@override_settings(AWS_BEDROCK_REGION="us-east-1")
class BedrockRerankTests(SimpleTestCase):
    def reranker(self, client):
        reranker = BedrockReranker()
        reranker._client = client
        reranker._region_cached = "us-east-1"
        return reranker

    def test_passages_are_ordered_by_the_models_scores(self):
        client = StubAgentRuntime(scores={0: 0.1, 1: 0.9})
        candidates = hits((chunk(1, "tangential"), 0.9), (chunk(2, "relevant"), 0.1))

        ranked = self.reranker(client).rerank("question", candidates)

        self.assertEqual([r.chunk.id for r in ranked], [2, 1])
        self.assertEqual(ranked[0].score, 0.9)
        arn = client.requests[0]["rerankingConfiguration"][
            "bedrockRerankingConfiguration"
        ]["modelConfiguration"]["modelArn"]
        self.assertEqual(
            arn, "arn:aws:bedrock:us-east-1::foundation-model/cohere.rerank-v3-5:0"
        )

    def test_an_outage_degrades_to_lexical_and_marks_every_passage(self):
        candidates = hits((chunk(1, "attendance rules"), 0.5))
        ranked = self.reranker(StubAgentRuntime(raises=ConnectionError())).rerank(
            "attendance", candidates
        )

        self.assertEqual(len(ranked), 1)
        self.assertIn("reranker_degraded", ranked[0].signals)

    def test_an_empty_result_set_is_a_failure_not_a_ranking(self):
        ranked = self.reranker(StubAgentRuntime(scores={})).rerank(
            "attendance", hits((chunk(1, "attendance rules"), 0.5))
        )
        self.assertIn("reranker_degraded", ranked[0].signals)


# --- Local sentence-transformer embeddings -----------------------------------


class FakeEncoded:
    def __init__(self, rows):
        self.rows = rows

    def tolist(self):
        return self.rows


class FakeSentenceTransformer:
    def __init__(self, width=384, rows=None):
        self.width = width
        self.rows = rows
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((list(texts), kwargs))
        if self.rows is not None:
            return FakeEncoded(self.rows)
        return FakeEncoded([[0.5] * self.width for _ in texts])


class LocalEmbeddingTests(SimpleTestCase):
    def provider(self, model):
        provider = LocalEmbeddingProvider()
        provider._model = model
        return provider

    def test_vectors_are_normalized_and_identify_the_model(self):
        model = FakeSentenceTransformer()
        batch = self.provider(model).embed(["attendance rules"])

        self.assertTrue(model.calls[0][1]["normalize_embeddings"])
        self.assertTrue(batch.semantic)
        self.assertEqual(batch.model_version, "BAAI/bge-small-en-v1.5")
        self.assertEqual(len(batch.vectors[0]), 384)

    def test_blank_texts_are_not_sent_to_the_model(self):
        model = FakeSentenceTransformer()
        batch = self.provider(model).embed(["", "real text", " "])

        self.assertEqual(model.calls[0][0], ["real text"])
        self.assertEqual(batch.vectors[0], [0.0] * 384)
        self.assertEqual(batch.vectors[2], [0.0] * 384)

    def test_a_wrong_width_vector_is_refused(self):
        with self.assertRaises(LocalEmbeddingUnavailable):
            self.provider(FakeSentenceTransformer(width=768)).embed(["text"])

    def test_a_short_batch_is_refused_rather_than_misaligned(self):
        """Two texts and one vector must not attach that vector to either."""
        model = FakeSentenceTransformer(rows=[[0.1] * 384])
        with self.assertRaises(LocalEmbeddingUnavailable):
            self.provider(model).embed(["one", "two"])


# --- Local cross-encoder -----------------------------------------------------


class FakeCrossEncoder:
    def __init__(self, scores):
        self.scores = scores
        self.pairs = []

    def predict(self, pairs, **kwargs):
        self.pairs = list(pairs)
        return FakeEncoded(list(self.scores[: len(pairs)]))


class FailingModel:
    def predict(self, *args, **kwargs):
        raise RuntimeError("model file is corrupt")


class LocalCrossEncoderTests(SimpleTestCase):
    def reranker(self, model):
        reranker = LocalCrossEncoderReranker()
        reranker._model = model
        return reranker

    def test_the_model_decides_the_order_not_retrieval(self):
        candidates = hits((chunk(1, "tangential"), 0.9), (chunk(2, "relevant"), 0.1))
        ranked = self.reranker(FakeCrossEncoder([0.05, 0.95])).rerank("q", candidates)

        self.assertEqual([r.chunk.id for r in ranked], [2, 1])

    def test_query_and_passage_are_scored_together(self):
        model = FakeCrossEncoder([0.5])
        self.reranker(model).rerank("the question", hits((chunk(1, "passage"), 1.0)))
        self.assertEqual(model.pairs, [("the question", "passage")])

    def test_logits_are_squashed_into_the_probability_range(self):
        """The floor is a probability; raw logits would make it meaningless."""
        ranked = self.reranker(FakeCrossEncoder([4.0, -4.0])).rerank(
            "q", hits((chunk(1, "a"), 1.0), (chunk(2, "b"), 1.0))
        )
        for item in ranked:
            self.assertGreaterEqual(item.score, 0.0)
            self.assertLessEqual(item.score, 1.0)
            self.assertIn("activation_repaired", item.signals)

    def test_an_empty_passage_is_ranked_last_and_says_why(self):
        ranked = self.reranker(FakeCrossEncoder([0.4])).rerank(
            "q", hits((chunk(1, "   "), 1.0), (chunk(2, "text"), 0.1))
        )
        self.assertEqual(ranked[-1].chunk.id, 1)
        self.assertFalse(ranked[-1].signals["scored"])

    def test_a_model_failure_degrades_and_marks_every_passage(self):
        ranked = self.reranker(FailingModel()).rerank(
            "attendance", hits((chunk(1, "attendance rules"), 1.0))
        )
        self.assertEqual(len(ranked), 1)
        self.assertIn("reranker_degraded", ranked[0].signals)


class AbstentionRecognitionTests(SimpleTestCase):
    """A model writes the abstention sentence the way a model writes it."""

    def test_quotes_apostrophes_and_a_missing_full_stop_still_abstain(self):
        from documents.rag.llm import ABSTENTION_TEXT, is_abstention

        for reply in (
            ABSTENTION_TEXT,
            f'"{ABSTENTION_TEXT}"',
            ABSTENTION_TEXT.replace("'", "’"),
            f"“{ABSTENTION_TEXT.rstrip('.')}”",
            f"  {ABSTENTION_TEXT}\n",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(is_abstention(reply))

    def test_an_answer_is_not_an_abstention(self):
        from documents.rag.llm import is_abstention

        self.assertFalse(is_abstention("Attendance must be 75 percent [1]."))
        self.assertFalse(
            is_abstention(
                "I couldn't find enough information about fees, but [1] says 75."
            )
        )
