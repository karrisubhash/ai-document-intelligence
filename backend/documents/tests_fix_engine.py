"""What the third review found in the engine, the gateway and the providers.

Each class pins one finding and the guard that keeps its fix from turning into
something worse: a quote that replaces a failed retry is checked like any other
answer; a reply that is passed over still counts against the day; an answer
from a model's own knowledge is only recognised when it cites nothing and
states no figure.
"""

import contextlib
import json
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from documents import agents, rag
from documents.agents import runner
from documents.observability import metrics
from documents.observability.logging import StructuredFormatter
from documents.rag import budget, engine, general, grounding, judging, streaming
from documents.rag import context as ctx
from documents.rag import gateway as gateway_module
from documents.rag.claude import SYSTEM, ClaudeAnswerer, ClaudeNotConfigured
from documents.rag.llm import ABSTENTION_TEXT, ExtractiveAnswerer, LLMResponse
from documents.rag.openai_compat import _usable
from documents.tests_llm_gateway import NO_KEYS, StatusError, build, wire
from documents.tests_providers import (
    FakeSearchResponse,
    answerer_with,
    completion,
)
from documents.tests_rag import chunk, hits
from documents.tests_rag_quality import (
    HOSTEL,
    THREE_PART,
    TUITION,
    FakeGateway,
    LowScoreReranker,
)

FIRST = "Tuition is 90000 per year [1]."
GOOD = "Tuition is 85000 per year [1]."


def chunk_for(index):
    return chunk(index, f"Passage number {index} about nothing in particular.")


class Scripted:
    """An OpenAI-shaped client that follows a script, one step per call.

    A step is a text, a (text, finish_reason) pair or an exception to raise;
    the last step repeats. `by_model` gives a model its own step instead. Asked
    to stream, it streams the words of the text and then the finish reason.
    """

    def __init__(self, *steps, by_model=None, usage=(120, 18), cost=None):
        self.steps = list(steps)
        self.by_model = by_model or {}
        self.usage = SimpleNamespace(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            **({"cost": cost} if cost is not None else {}),
        )
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _step(self, model):
        if model in self.by_model:
            return self.by_model[model]
        return self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._step(kwargs["model"])
        if isinstance(step, BaseException):
            raise step
        text, finish = (step, "stop") if isinstance(step, str) else step
        if kwargs.get("stream"):
            words = [word + " " for word in text.split()]
            pieces = [
                SimpleNamespace(
                    model="stub",
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=word), finish_reason=None
                        )
                    ],
                )
                for word in words
            ]
            pieces.append(
                SimpleNamespace(
                    model="stub",
                    usage=self.usage,
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=None), finish_reason=finish
                        )
                    ],
                )
            )
            return iter(pieces)
        done = completion(text, finish)
        done.usage = self.usage
        return done


class FreshBudget:
    """No call counted, no link benched, no developer's key in the way."""

    def setUp(self):
        super().setUp()
        cache.clear()
        budget.reset()
        patcher = mock.patch.dict("os.environ", NO_KEYS)
        patcher.start()
        self.addCleanup(patcher.stop)


def recoveries():
    return {
        outcome: metrics.REGISTRY.get_sample_value(
            "adi_rag_grounding_recoveries_total", {"outcome": outcome}
        )
        or 0
        for outcome in (
            "repaired",
            "retried_grounded",
            "retried_failed",
            "retry_unavailable",
        )
    }


class EngineHarness(FreshBudget):
    """rag.ask over fake search results, answered by `answerer`."""

    def ask(self, answerer, *passages, scores=None, question=None, sink=None):
        found = hits(
            *[
                (chunk(n, text), 1.0 - n / 10)
                for n, text in enumerate(passages, start=1)
            ]
        )
        # Above the floor unless the test says otherwise.
        scores = (
            dict.fromkeys(range(1, len(passages) + 1), 0.9)
            if scores is None
            else scores
        )
        capture = streaming.capture(sink) if sink else contextlib.nullcontext()
        with (
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(
                engine, "get_reranker", return_value=LowScoreReranker(scores)
            ),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
            capture,
        ):
            return rag.ask(
                None, question or "What is the tuition per year for the programme?"
            )


# --- engine/F3, deploy/F5: a retry that never reached a model ----------------


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class RetryUnavailableTests(EngineHarness, TestCase):
    """The corrective retry failed to REACH a model: a timeout, a 429 on every
    link. That is an outage, and it used to be recorded as the model failing to
    ground an answer, with the quote the answerer had just produced thrown away."""

    def test_the_quote_the_retry_fell_back_to_is_checked_and_shown(self):
        client = Scripted(FIRST, TimeoutError("timed out"))

        answer = self.ask(client_answerer(client), TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("85000", answer.answer)
        self.assertNotIn("90000", answer.answer)
        self.assertEqual([c["marker"] for c in answer.citations], [1])
        self.assertTrue(answer.answerer_fell_back)
        self.assertFalse(answer.generative)
        self.assertEqual(answer.llm["fallback_reason"], "TimeoutError")
        self.assertEqual(answer.llm["fallback_kind"], "error")

    def test_the_note_says_the_retry_was_unavailable_not_that_the_model_failed(self):
        answer = self.ask(
            client_answerer(Scripted(FIRST, TimeoutError("timed out"))), TUITION
        )

        notes = " | ".join(answer.notes)
        self.assertIn("the retry was unavailable (TimeoutError)", notes)
        # The wording labels.degraded() reads, so the reader is told the AI
        # could not write this answer rather than "not enough evidence".
        self.assertIn("answered by quoting passages instead", notes)
        self.assertNotIn("did not produce a grounded answer", notes)

    def test_the_first_call_is_still_paid_for(self):
        answer = self.ask(
            client_answerer(Scripted(FIRST, TimeoutError("timed out"))), TUITION
        )

        # One reply was received (the stub reports 120 and 18); the failed
        # retry used nothing that was reported.
        self.assertEqual(answer.llm["prompt_tokens"], 120)
        self.assertEqual(answer.llm["completion_tokens"], 18)

    def test_an_outage_is_counted_apart_from_a_model_that_failed_twice(self):
        before = recoveries()

        self.ask(client_answerer(Scripted(FIRST, TimeoutError("timed out"))), TUITION)
        self.ask(client_answerer(Scripted(FIRST, StatusError(429))), TUITION)

        after = recoveries()
        self.assertEqual(after["retry_unavailable"] - before["retry_unavailable"], 2)
        self.assertEqual(after["retried_failed"] - before["retried_failed"], 0)

    def test_a_model_that_answers_wrongly_twice_is_still_retried_failed(self):
        before = recoveries()

        answer = self.ask(
            client_answerer(Scripted(FIRST, "Tuition is 95000 per year [1].")), TUITION
        )

        after = recoveries()
        self.assertEqual(after["retried_failed"] - before["retried_failed"], 1)
        self.assertEqual(after["retry_unavailable"], before["retry_unavailable"])
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_rate_limit_on_the_retry_is_capacity_that_comes_back(self):
        answer = self.ask(client_answerer(Scripted(FIRST, StatusError(429))), TUITION)

        self.assertFalse(answer.abstained)
        self.assertEqual(answer.llm["fallback_kind"], "capacity")

    def test_a_quote_that_fails_the_check_leaves_the_first_failure_standing(self):
        """The quote is checked like any answer: when it does not pass, the
        first failure stands - and the outage is still recorded honestly."""
        real = grounding.validate

        def reject_the_quote(text, context, **kwargs):
            if "for the programme" in text:
                report = grounding.GroundingReport(grounded=False, text=text)
                report.failures.append("a figure the passages do not state")
                return report
            return real(text, context, **kwargs)

        client = Scripted(FIRST, TimeoutError("timed out"))
        with mock.patch.object(grounding, "validate", side_effect=reject_the_quote):
            answer = self.ask(client_answerer(client), TUITION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertNotIn("85000", answer.answer)
        notes = " | ".join(answer.notes)
        self.assertIn("the retry was unavailable (TimeoutError)", notes)
        self.assertIn("first failure stands", notes)
        self.assertEqual(answer.llm["retry_fallback_reason"], "TimeoutError")
        self.assertEqual(answer.llm["retry_fallback_kind"], "error")

    def test_a_quote_that_finds_nothing_is_not_adopted(self):
        """A fallback that has nothing to quote returns the abstention text,
        which passes the check; it must not be taken for an answer."""
        client = Scripted(FIRST, TimeoutError("timed out"))
        with mock.patch.object(
            ExtractiveAnswerer,
            "answer",
            return_value=LLMResponse(
                text=ABSTENTION_TEXT, abstained=True, model_name="extractive"
            ),
        ):
            answer = self.ask(client_answerer(client), TUITION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_streaming_reader_is_given_a_reset_and_then_the_quote_in_done(self):
        events = []

        def sink(event, payload):
            events.append(event)

        answer = self.ask(
            client_answerer(Scripted(FIRST, TimeoutError("timed out"))),
            TUITION,
            sink=sink,
        )

        self.assertFalse(answer.abstained)
        self.assertIn("reset", events)
        self.assertGreater(events.index("reset"), events.index("status"))


def client_answerer(client, **overrides):
    return answerer_with(client, **overrides)


# --- gateway/GW-1, engine/F6: what two calls cost and who saw the documents ---


class TwoCallRecordTests(EngineHarness, TestCase):
    """The retry is served by a different link when the first is used up by
    then. The record keeps both: tokens, cost and the providers that may
    train on what they were sent."""

    def two_links(self, *, first, second, cost=None):
        answerer = answerer_with(
            None,
            RAG_ANSWERER_CHAIN="gemini:gemini-3.5-flash,groq",
            LLM_DAILY_LIMITS="gemini=1",
        )
        wire(answerer.gateway, "gemini", Scripted(*first, cost=cost))
        wire(answerer.gateway, "groq", Scripted(*second, cost=cost))
        return answerer

    def test_the_first_provider_that_may_train_is_not_forgotten(self):
        answerer = self.two_links(first=[FIRST], second=[GOOD])

        answer = self.ask(answerer, TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(answer.llm["provider"], "groq")
        self.assertEqual(answer.llm["first_provider"], "gemini")
        # Groq's free tier does not train; Gemini's does, and was sent the
        # question and the passages first.
        self.assertTrue(answer.llm["may_train_on_input"])

    def test_the_flag_survives_a_retry_that_fails_too(self):
        answerer = self.two_links(first=[FIRST], second=["Tuition is 95000 [1]."])

        answer = self.ask(answerer, TUITION)

        self.assertTrue(answer.abstained)
        self.assertTrue(answer.llm["may_train_on_input"])

    def test_a_provider_that_never_trains_stays_false(self):
        answerer = answerer_with(None, RAG_ANSWERER_CHAIN="groq", LLM_DAILY_LIMITS="")
        wire(answerer.gateway, "groq", Scripted(FIRST, GOOD))

        answer = self.ask(answerer, TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertFalse(answer.llm["may_train_on_input"])

    def test_cost_is_summed_over_both_calls_like_tokens(self):
        answerer = self.two_links(first=[FIRST], second=[GOOD], cost=0.01)

        answer = self.ask(answerer, TUITION)

        self.assertEqual(answer.llm["prompt_tokens"], 240)
        self.assertEqual(answer.llm["completion_tokens"], 36)
        self.assertAlmostEqual(answer.llm["cost"], 0.02)

    def test_a_cost_nobody_reported_is_not_invented(self):
        answerer = self.two_links(first=[FIRST], second=[GOOD])

        answer = self.ask(answerer, TUITION)

        self.assertNotIn("cost", answer.llm)


class JoinedRecordTests(SimpleTestCase):
    def test_one_call_that_reported_a_cost_is_not_doubled(self):
        first = {"prompt_tokens": 10, "completion_tokens": 2, "provider": "a"}
        second = {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.5}

        joined = engine._joined_llm(first, second, second)

        self.assertEqual(joined["cost"], 0.5)
        self.assertEqual(joined["prompt_tokens"], 15)
        self.assertFalse(joined["may_train_on_input"])
        self.assertEqual(joined["first_provider"], "a")


# --- engine/F5: a quote after a model failure is measured on what it always was


def passage(marker, text, *, judged=False):
    return ctx.Passage(
        marker=marker,
        chunk=SimpleNamespace(id=marker),
        text=text,
        relevance=0.9,
        model_judged=judged,
    )


class QuoteContextTests(SimpleTestCase):
    def context(self, *passages):
        return ctx.BuiltContext(
            passages=list(passages),
            characters=sum(len(p.text) for p in passages),
        )

    def test_a_quote_gets_no_more_passages_than_the_quoting_path_always_had(self):
        built = self.context(*[passage(n, f"Passage {n}.") for n in range(1, 13)])

        quoted = judging.quote_context(built)

        self.assertEqual([p.marker for p in quoted.passages], list(range(1, 9)))

    @override_settings(RAG_MAX_PASSAGES=3)
    def test_the_cap_is_the_setting(self):
        built = self.context(*[passage(n, f"Passage {n}.") for n in range(1, 8)])

        self.assertEqual(
            [p.marker for p in judging.quote_context(built).passages], [1, 2, 3]
        )

    @override_settings(RAG_CONTEXT_BUDGET=100)
    def test_the_budget_is_the_setting_and_a_smaller_passage_later_still_fits(self):
        built = self.context(
            passage(1, "a" * 60),
            passage(2, "b" * 60),
            passage(3, "c" * 30),
        )

        quoted = judging.quote_context(built)

        self.assertEqual([p.marker for p in quoted.passages], [1, 3])
        self.assertEqual(quoted.characters, 90)

    def test_a_passage_only_a_model_was_asked_to_judge_is_never_quoted(self):
        built = self.context(
            passage(1, "Vouched for."),
            passage(2, "Judged by the model only.", judged=True),
            passage(3, "Also vouched for."),
        )

        quoted = judging.quote_context(built)

        # Markers are kept, so a quoted sentence still resolves in grounding.
        self.assertEqual([p.marker for p in quoted.passages], [1, 3])

    def test_a_small_context_is_handed_back_unchanged(self):
        built = self.context(passage(1, "One."), passage(2, "Two."))

        self.assertIs(judging.quote_context(built), built)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class FallbackQuoteIsMeasuredTests(EngineHarness, TestCase):
    def test_the_fallback_quoter_sees_the_old_budget_not_the_models(self):
        passages = [
            f"Handbook section {n} describes procedure {n} of the registry office."
            for n in range(1, 13)
        ]
        seen = []
        real = ExtractiveAnswerer.answer

        def spy(self, *, question, context, prompt):
            seen.append(context)
            return real(self, question=question, context=context, prompt=prompt)

        with mock.patch.object(ExtractiveAnswerer, "answer", spy):
            answer = self.ask(
                client_answerer(Scripted(TimeoutError("timed out"))), *passages
            )

        self.assertTrue(answer.answerer_fell_back)
        # The model was shown all twelve; the quote is not.
        self.assertEqual(answer.passages_used, 12)
        self.assertEqual(len(seen), 1)
        self.assertEqual([p.marker for p in seen[0].passages], list(range(1, 9)))


# --- engine/F1: a compound question the model failed on is planned after all --


def failed_whole(question, reason="ANSWERER_FAILED", fell_back=True):
    return rag.RagAnswer(
        question=question,
        answer=ABSTENTION_TEXT,
        abstained=True,
        abstention_reason=getattr(engine.Abstention, reason),
        answerer_fell_back=fell_back,
        model_name="extractive",
        llm={
            "fell_back_from": "groq/x",
            "fallback_reason": "TimeoutError",
            "fallback_kind": "error",
            "prompt_tokens": 7,
            "completion_tokens": 0,
            "may_train_on_input": True,
        },
        notes=["generative answerer openai-compatible was unavailable (TimeoutError)"],
        timings={"generation": 11},
    )


def quoted_part(question):
    return rag.RagAnswer(
        question=question,
        answer="The answer is in the passage [1].",
        abstained=False,
        citations=[{"marker": 1, "document_title": "Doc"}],
        model_name="extractive",
        support_ratio=1.0,
    )


class PlannedFallbackTests(SimpleTestCase):
    def go(self, whole):
        calls = []

        def ask(user, question, **kwargs):
            calls.append((question, kwargs))
            if question == THREE_PART:
                return whole
            return quoted_part(question)

        model = mock.Mock(generative=True, gateway=FakeGateway(True))
        with (
            mock.patch.object(runner, "ask", side_effect=ask),
            mock.patch.object(runner, "get_answerer", return_value=model),
        ):
            return calls, agents.answer(None, THREE_PART)

    def test_a_model_that_failed_on_the_whole_question_leaves_the_parts_to_a_quote(
        self,
    ):
        calls, planned = self.go(failed_whole(THREE_PART))

        self.assertEqual(calls[0][0], THREE_PART)
        self.assertEqual(len(calls), 4)
        # The parts are answered by a quote-only answerer: no further model call.
        for _, kwargs in calls[1:]:
            self.assertIsInstance(kwargs["answerer"], ExtractiveAnswerer)
        self.assertNotIn("answerer", calls[0][1])
        self.assertTrue(planned.planned)
        self.assertFalse(planned.abstained)
        self.assertEqual(len(planned.steps), 3)

    def test_what_the_model_attempt_used_is_still_reported(self):
        _, planned = self.go(failed_whole(THREE_PART))

        self.assertTrue(planned.answerer_fell_back)
        self.assertEqual(planned.llm["fallback_reason"], "TimeoutError")
        self.assertEqual(planned.llm["prompt_tokens"], 7)
        self.assertTrue(planned.llm["may_train_on_input"])
        self.assertEqual(planned.timings["generation"], 11)
        notes = " | ".join(planned.notes)
        self.assertIn("went to the model whole", notes)
        self.assertIn("unavailable (TimeoutError)", notes)
        # What answered is the quote.
        self.assertFalse(planned.generative)

    def test_a_reply_that_was_cut_off_is_a_failure_to_plan_for_too(self):
        _, planned = self.go(failed_whole(THREE_PART, fell_back=False))

        self.assertTrue(planned.planned)

    def test_a_model_that_answered_is_not_second_guessed(self):
        answered = rag.RagAnswer(
            question=THREE_PART,
            answer="All three, in one answer [1].",
            abstained=False,
            model_name="openai-compatible",
            generative=True,
        )

        calls, planned = self.go(answered)

        self.assertEqual([question for question, _ in calls], [THREE_PART])
        self.assertFalse(planned.planned)

    def test_a_model_that_found_nothing_is_not_second_guessed_either(self):
        """MODEL_ABSTAINED is the model saying the passages do not answer it:
        that is an answer, and cutting the question up spends more calls."""
        refused = rag.RagAnswer(
            question=THREE_PART,
            answer=ABSTENTION_TEXT,
            abstained=True,
            abstention_reason=engine.Abstention.MODEL_ABSTAINED,
            model_name="openai-compatible",
            generative=True,
        )

        calls, planned = self.go(refused)

        self.assertEqual(len(calls), 1)
        self.assertFalse(planned.planned)

    def test_a_grounding_failure_is_not_second_guessed(self):
        failed = rag.RagAnswer(
            question=THREE_PART,
            answer=ABSTENTION_TEXT,
            abstained=True,
            abstention_reason=engine.Abstention.UNGROUNDED,
            model_name="openai-compatible",
            generative=True,
        )

        calls, _ = self.go(failed)

        self.assertEqual(len(calls), 1)

    def test_a_rejected_draft_whose_retry_never_reached_a_model_is_planned_for(self):
        """The first failure stands, but it is an outage that kept the model from
        trying again, and a quote of the WHOLE compound question covers one part
        at best: the parts are quoted, as when the first call itself fails."""
        rejected = rag.RagAnswer(
            question=THREE_PART,
            answer=ABSTENTION_TEXT,
            abstained=True,
            abstention_reason=engine.Abstention.UNGROUNDED,
            model_name="openai-compatible",
            generative=True,
            llm={
                "retry_fallback_reason": "TimeoutError",
                "retry_fallback_kind": "error",
            },
        )

        calls, planned = self.go(rejected)

        self.assertEqual(len(calls), 4)
        self.assertTrue(planned.planned)
        self.assertFalse(planned.abstained)
        self.assertEqual(len(planned.steps), 3)


P1 = (
    "A student must maintain a minimum of 75 percent attendance in each course "
    "to be eligible for the end-semester examination."
)
P2 = "A student may borrow up to 4 books from the library at one time for 14 days."
P3 = "The hostel curfew is 10 pm on weekdays and 11 pm on weekends for residents."
P4 = (
    "A library book returned late attracts a fine of 5 rupees per day after the "
    "due date."
)
P5 = "The annual bus pass costs 12000 rupees and is valid for all campus routes."
FOUR = (
    "What is the attendance minimum, what is the late return fine, what is "
    "the hostel curfew, and what does the bus pass cost?"
)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class PlannedFallbackEndToEndTests(FreshBudget, TestCase):
    """The reviewer's four-part question, through the real engine: the base
    planned it and answered all four; the branch sent it whole to a model that
    timed out and refused it."""

    def go(self, answerer, question=FOUR):
        found = hits(
            (chunk(1, P1, "Exam Rules"), 1.0),
            (chunk(2, P2, "Library"), 0.9),
            (chunk(3, P3, "Hostel"), 0.8),
            (chunk(4, P4, "Library fines"), 0.7),
            (chunk(5, P5, "Transport"), 0.6),
        )
        with (
            mock.patch.object(
                engine.search_service,
                "search",
                side_effect=lambda user, q, **kw: FakeSearchResponse(found),
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(runner, "get_answerer", return_value=answerer),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
        ):
            return agents.answer(None, question)

    def test_a_four_part_question_is_answered_in_full_after_the_model_times_out(self):
        client = Scripted(TimeoutError("timed out"))

        planned = self.go(client_answerer(client))

        self.assertTrue(planned.planned)
        self.assertFalse(planned.abstained, planned.notes)
        self.assertEqual(len(planned.steps), 4)
        self.assertEqual([step.abstained for step in planned.steps], [False] * 4)
        for figure in ("75 percent", "5 rupees", "10 pm", "12000"):
            self.assertIn(figure, planned.answer)
        # One call, for the whole question: the parts spent none.
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(planned.answerer_fell_back)

    def test_a_wrong_first_draft_and_a_retry_that_cannot_run_still_answers_all_four(
        self,
    ):
        """The realistic free-tier case: the model states a figure the passage does
        not (90 percent for 75), and the corrective retry is refused (a timeout,
        or a 429 inside the same minute). It used to be refused whole."""
        wrong = (
            "The attendance minimum is 90 percent [1], the late fine is 5 rupees a "
            "day [4], the hostel curfew is 10 pm [3] and the bus pass costs 12000 "
            "rupees [5]."
        )
        for failure in (TimeoutError("timed out"), StatusError(429)):
            with self.subTest(failure=type(failure).__name__):
                cache.clear()
                budget.reset()
                client = Scripted(wrong, failure)

                planned = self.go(client_answerer(client))

                self.assertTrue(planned.planned, planned.notes)
                self.assertFalse(planned.abstained, planned.notes)
                self.assertEqual(len(planned.steps), 4)
                self.assertEqual([s.abstained for s in planned.steps], [False] * 4)
                for figure in ("75 percent", "5 rupees", "10 pm", "12000"):
                    self.assertIn(figure, planned.answer)
                self.assertNotIn("90 percent", planned.answer)
                # The whole question's call and its retry: the parts spent none.
                self.assertEqual(len(client.calls), 2)


# --- engine/F1, optional: a Claude answerer with no way to reach a model ------


class ClaudeWithoutAClientTests(SimpleTestCase):
    def test_an_answerer_that_cannot_build_a_client_will_not_answer(self):
        answerer = ClaudeAnswerer()

        with mock.patch.object(
            ClaudeAnswerer, "_get_client", side_effect=ClaudeNotConfigured("no sdk")
        ):
            self.assertFalse(judging.model_will_answer(answerer))

    def test_one_that_can_will(self):
        answerer = ClaudeAnswerer()
        answerer._client = object()

        self.assertTrue(judging.model_will_answer(answerer))

    def test_a_compound_question_is_planned_for_it_like_a_quote(self):
        answerer = ClaudeAnswerer()
        calls = []

        def ask(user, question, **kwargs):
            calls.append(question)
            return rag.RagAnswer(
                question=question, answer=ABSTENTION_TEXT, abstained=True
            )

        with (
            mock.patch.object(
                ClaudeAnswerer, "_get_client", side_effect=ClaudeNotConfigured("no sdk")
            ),
            mock.patch.object(runner, "ask", side_effect=ask),
            mock.patch.object(runner, "get_answerer", return_value=answerer),
        ):
            planned = agents.answer(None, THREE_PART)

        self.assertEqual(len(calls), 3)
        self.assertTrue(planned.planned)

    def test_a_double_with_no_such_method_is_still_a_model(self):
        self.assertTrue(
            judging.model_will_answer(mock.Mock(spec=["generative"], generative=True))
        )


# --- deploy/F4(a): a reply with nothing to use is that link failing -----------

CHAT_REQUEST = {"messages": [{"role": "user", "content": "q"}]}


def run(chain, **kwargs):
    return chain.run(
        lambda link: {"model": link.model, **CHAT_REQUEST},
        acceptable=_usable,
        **kwargs,
    )


class UnusableReplyTests(FreshBudget, SimpleTestCase):
    def two(self, first, second):
        chain = build("gemini:gemini-3.5-flash,groq")
        gemini = wire(chain, "gemini", Scripted(first))
        groq = wire(chain, "groq", Scripted(second))
        return chain, gemini, groq

    def test_a_reply_cut_off_at_the_limit_moves_the_walk_to_the_next_link(self):
        chain, gemini, groq = self.two(("Half an ans", "length"), "A full answer [1].")

        result = run(chain)

        self.assertEqual(result.text, "A full answer [1].")
        self.assertEqual(result.link.provider, "groq")
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(groq.calls), 1)
        self.assertEqual(result.fell_back_from, "gemini/gemini-3.5-flash")

    def test_an_empty_reply_moves_on_whatever_its_finish_reason(self):
        for finish in ("stop", "length", "content_filter"):
            with self.subTest(finish=finish):
                cache.clear()
                budget.reset()
                chain, gemini, groq = self.two(("", finish), "Answer [1].")

                self.assertEqual(run(chain).text, "Answer [1].")

    def test_a_filtered_reply_moves_on_too(self):
        chain, _, _ = self.two(("Some text", "content_filter"), "Answer [1].")

        self.assertEqual(run(chain).text, "Answer [1].")

    def test_the_unusable_request_stays_counted_against_the_day(self):
        chain, _, _ = self.two(("", "stop"), "Answer [1].")

        run(chain)

        states = chain.broker.states(chain.links)
        # It was served: refunding it would let a model that keeps returning
        # nothing be asked all day.
        self.assertEqual(states["gemini:gemini-3.5-flash"].calls_today, 1)
        self.assertEqual(states["groq:openai/gpt-oss-120b"].calls_today, 1)

    def test_the_link_is_not_marked_as_having_answered_cleanly(self):
        chain, _, _ = self.two(("", "stop"), "Answer [1].")

        run(chain)

        states = chain.broker.states(chain.links)
        gemini = states["gemini:gemini-3.5-flash"]
        self.assertIsNone(gemini.last_success_at)
        self.assertEqual(gemini.last_error["type"], "UnusableCompletion")
        self.assertIsNotNone(states["groq:openai/gpt-oss-120b"].last_success_at)

    def test_the_link_is_not_benched_the_next_question_may_try_it_first(self):
        chain, _, _ = self.two(("", "stop"), "Answer [1].")

        run(chain)

        plan = chain.plan(gateway_module.CHAT)
        self.assertEqual(plan.links[0].provider, "gemini")

    def test_the_other_models_on_the_same_account_are_still_tried(self):
        """One Gemini endpoint serves four models: a thinking model that spent
        its cap returns nothing, and the next model there may not."""
        chain = build("gemini")
        client = wire(
            chain,
            "gemini",
            Scripted("Answer [1].", by_model={"gemini-3.5-flash": ("", "length")}),
        )

        result = run(chain)

        self.assertEqual(result.text, "Answer [1].")
        self.assertEqual(
            [call["model"] for call in client.calls],
            ["gemini-3.5-flash", "gemini-3-flash-preview"],
        )

    def test_when_every_link_is_unusable_the_last_reply_is_returned_not_raised(self):
        chain, gemini, groq = self.two(("", "length"), ("Cut off", "length"))

        result = run(chain)

        # The caller reports it exactly as it always did.
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.link.provider, "groq")
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(groq.calls), 1)

    def test_without_a_test_the_first_reply_is_returned_as_before(self):
        chain, gemini, groq = self.two(("", "length"), "Answer [1].")

        result = chain.run(lambda link: {"model": link.model, **CHAT_REQUEST})

        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(len(groq.calls), 0)

    def test_what_the_passed_over_reply_used_is_part_of_the_answer_cost(self):
        chain = build("gemini:gemini-3.5-flash,groq")
        wire(chain, "gemini", Scripted(("", "stop"), usage=(1000, 50)))
        wire(chain, "groq", Scripted("Answer [1].", usage=(120, 18)))

        result = run(chain)
        response = gateway_module.response_from(
            result, text=result.text, name="x", prompt_characters=1
        )

        self.assertEqual(response.prompt_tokens, 1120)
        self.assertEqual(response.completion_tokens, 68)

    def test_a_streamed_unusable_reply_is_withdrawn_before_the_next_link_starts(self):
        chain, _, _ = self.two(("Half an ans", "length"), "A full answer [1].")
        shown, resets = [], []

        result = run(
            chain, on_delta=shown.append, on_reset=lambda: resets.append(len(shown))
        )

        self.assertEqual(result.text.strip(), "A full answer [1].")
        # The half-answer reached the reader, then was withdrawn before the
        # second link's words.
        self.assertEqual(resets, [3])

    def test_text_that_cannot_be_withdrawn_is_not_followed_by_another_model(self):
        chain, _, groq = self.two(("Half an ans", "length"), "A full answer [1].")

        result = run(chain, on_delta=lambda piece: None)

        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(len(groq.calls), 0)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class UnusableReplyThroughTheAnswererTests(EngineHarness, TestCase):
    def chained(self, first, second, **settings):
        answerer = answerer_with(
            None,
            RAG_ANSWERER_CHAIN="gemini:gemini-3.5-flash,groq",
            LLM_DAILY_LIMITS="",
            **settings,
        )
        gemini = wire(answerer.gateway, "gemini", Scripted(first, usage=(900, 40)))
        groq = wire(answerer.gateway, "groq", Scripted(second))
        return answerer, gemini, groq

    def test_a_cut_off_first_link_no_longer_ends_the_request(self):
        answerer, gemini, groq = self.chained(("Tuition", "length"), GOOD)

        answer = self.ask(answerer, TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(answer.answer, GOOD)
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(groq.calls), 1)

    def test_the_documents_went_to_the_link_that_was_passed_over(self):
        answerer, _, _ = self.chained(("", "stop"), GOOD)

        answer = self.ask(answerer, TUITION)

        # Groq answered and does not train; Gemini was sent the passages and
        # may, and the record says so.
        self.assertEqual(answer.llm["provider"], "groq")
        self.assertTrue(answer.llm["may_train_on_input"])
        self.assertEqual(answer.llm["prompt_tokens"], 1020)

    def test_when_the_last_link_is_unusable_too_the_failure_is_reported_as_before(
        self,
    ):
        answerer, _, _ = self.chained(("", "length"), ("Cut", "length"))

        answer = self.ask(answerer, TUITION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.ANSWERER_FAILED)
        self.assertTrue(any("cut off" in note for note in answer.notes), answer.notes)


# --- deploy/F3: the answer language -----------------------------------------


class AnswerLanguageTests(SimpleTestCase):
    def test_the_prompt_asks_for_the_language_of_the_passages(self):
        flattened = " ".join(SYSTEM.split())

        self.assertIn("in the language the passages are written in", flattened)
        self.assertIn("even when the question is asked in another language", flattened)

    def test_both_answerers_share_the_one_prompt(self):
        from documents.rag import openai_compat

        self.assertIs(openai_compat.SYSTEM, SYSTEM)

    def test_the_rule_reaches_a_provider_in_the_request(self):
        client = Scripted(GOOD)
        answerer = client_answerer(client, RAG_ANSWERER_PROVIDER="groq")
        built = ctx.build(
            LowScoreReranker({1: 0.9}).rerank("q", hits((chunk(1, TUITION), 1.0))),
            semantic_scores=True,
        )

        answerer.answer(question="q", context=built, prompt="PROMPT")

        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("language the passages are written in", " ".join(system.split()))


# --- deploy/F2: an answer from the model's own knowledge ---------------------

CHAT_QUESTIONS = {
    "capital": "What is the capital of France?",
    "leap": "How many days are in a leap year?",
    "python": "What is a Python list comprehension?",
}


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class OwnKnowledgeTests(EngineHarness, TestCase):
    """Weak passages are put in front of a model so it can judge them; a model
    that ignores them and answers a general question from what it knows has
    declined the documents, and chat's general reply may then answer."""

    def weak(self, reply, *passages, question=None, scores=None):
        passages = passages or (TUITION, HOSTEL)
        # Nothing passes the floor: every passage is only there to be judged.
        scores = {} if scores is None else scores
        return self.ask(
            client_answerer(Scripted(reply)),
            *passages,
            scores=scores,
            question=question or CHAT_QUESTIONS["capital"],
        )

    def test_an_uncited_answer_to_unrelated_passages_is_a_refusal_of_the_documents(
        self,
    ):
        answer = self.weak("Paris is the capital of France.")

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)
        self.assertIn(answer.abstention_reason, general.FALLBACK_REASONS)
        self.assertTrue(
            any("answered without citing a passage" in n for n in answer.notes),
            answer.notes,
        )

    def test_a_longer_explanation_with_no_figure_is_one_too(self):
        answer = self.weak(
            "A list comprehension builds a list from an iterable in one "
            "expression, applying a filter as it goes.",
            question=CHAT_QUESTIONS["python"],
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)

    def test_an_answer_that_cites_a_passage_is_still_checked(self):
        answer = self.weak("Paris is the capital of France [1].")

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertNotIn(answer.abstention_reason, general.FALLBACK_REASONS)

    def test_an_answer_that_cites_a_passage_that_does_not_exist_is_still_checked(
        self,
    ):
        answer = self.weak("Paris is the capital of France [7].")

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_an_answer_that_states_a_figure_is_still_checked(self):
        answer = self.weak("A leap year has 366 days.", question=CHAT_QUESTIONS["leap"])

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_date_or_an_identifier_is_a_figure_too(self):
        for reply in ("It was founded on 15 August.", "Use form REG/2024/117."):
            with self.subTest(reply=reply):
                answer = self.weak(reply)

                self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_true_answer_that_forgot_its_citation_is_not_called_not_found(self):
        """Its words are the passage's: it is an answer from the documents,
        and 'your documents do not cover this' would be false."""
        answer = self.weak(
            "Tuition for the programme is paid every year.",
            TUITION,
            question="How is the tuition paid?",
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_with_a_passage_the_reranker_vouched_for_the_check_stands(self):
        """Evidence existed, so an uncited reply is not 'from its own
        knowledge about nothing': it stays on the checked path."""
        answer = self.weak(
            "Paris is the capital of France.",
            TUITION,
            HOSTEL,
            scores={1: 0.9},
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_reply_too_short_to_measure_stays_checked(self):
        """ "No." answers a yes/no question whichever passage it came from, and
        one content word found in no passage proves nothing."""
        for reply in ("No.", "Yes, it is.", "Paris."):
            with self.subTest(reply=reply):
                answer = self.weak(reply, question="Who wrote Hamlet?")

                self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_long_true_answer_that_forgot_its_citation_is_not_called_not_found(self):
        """Enough words to measure, and most of them the passage's own."""
        passage = (
            "Tuition for the programme is 85000 per year, paid in two equal "
            "instalments before each semester begins."
        )

        answer = self.weak(
            "Tuition for the programme is usually paid every year in two equal "
            "instalments, normally due before each semester begins.",
            passage,
            question="How is the tuition paid?",
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_short_true_paraphrase_that_forgot_its_citation_is_not_called_not_found(
        self,
    ):
        """Natural wording shares under half its words with the passage (0.45
        here), and "your documents do not cover this" about a refund the document
        states would be false: it stays an ungrounded draft."""
        passage = (
            "Refund of tuition fees is permitted for withdrawals made before the "
            "commencement of classes. A processing charge is deducted from the "
            "amount refunded."
        )

        answer = self.weak(
            "Yes. If you withdraw before classes begin you are entitled to a "
            "refund, less a processing charge.",
            passage,
            question="Can I get my fees back if I leave before the term starts?",
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertNotIn(answer.abstention_reason, general.FALLBACK_REASONS)

    def test_a_failing_probe_leaves_the_answer_ungrounded(self):
        real = grounding.validate
        calls = {"n": 0}

        def second_call_raises(text, context, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("boom")
            return real(text, context, **kwargs)

        with mock.patch.object(grounding, "validate", side_effect=second_call_raises):
            answer = self.weak("Paris is the capital of France.")

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)


# --- deploy/F6(c): a failing check logs what failed, not what it chewed on ----


class LogRedactionTests(SimpleTestCase):
    SECRET = "TUITION FOR THE PHD PROGRAMME IS 123456"

    def line_for(self, raiser, call):
        with (
            mock.patch.object(grounding, raiser, side_effect=KeyError(self.SECRET)),
            self.assertLogs("adi.rag", level="ERROR") as logs,
        ):
            call()
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        return StructuredFormatter().format(record), record

    def test_the_check_logs_its_type_and_place_not_its_message(self):
        line, record = self.line_for(
            "validate", lambda: engine._check("An answer.", None)
        )

        payload = json.loads(line)
        self.assertNotIn(self.SECRET, line)
        self.assertNotIn("123456", line)
        self.assertEqual(payload["message"], "grounding.check_failed")
        self.assertEqual(payload["error"], "KeyError")
        self.assertIn(":", payload["where"])
        # No traceback at all: its last line is the exception's message.
        self.assertNotIn("exception", payload)
        self.assertIsNone(record.exc_info)

    def test_the_repair_does_the_same(self):
        report = grounding.GroundingReport(text="x")

        line, _ = self.line_for(
            "repair_citations", lambda: engine._repair(report, None)
        )

        payload = json.loads(line)
        self.assertNotIn(self.SECRET, line)
        self.assertEqual(payload["message"], "grounding.repair_failed")
        self.assertEqual(payload["error"], "KeyError")
        self.assertNotIn("exception", payload)

    def test_a_failure_still_withholds_the_answer(self):
        with (
            mock.patch.object(grounding, "validate", side_effect=KeyError("x")),
            self.assertLogs("adi.rag", level="ERROR"),
        ):
            report = engine._check("Tuition is 85000 [1].", None)

        self.assertFalse(report.ok)

    def test_the_place_is_short_enough_for_the_log_formatter_to_keep(self):
        try:
            raise KeyError(self.SECRET)
        except KeyError as exc:
            where = engine._where(exc)

        self.assertLessEqual(len(where), 120)
        self.assertNotIn(self.SECRET, where)


# --- gateway/GW-2: one global effort knob must not 400 half the chain ---------


def efforts(chain, **settings):
    """{link id: reasoning_effort sent, or None}."""
    links = build(
        chain, **{"RAG_ANSWERER_MODEL": "", "RAG_ANSWERER_BASE_URL": "", **settings}
    ).links
    return {
        link.id: dict(link.request_extras).get("reasoning_effort") for link in links
    }


class ReasoningEffortGuardTests(SimpleTestCase):
    def test_the_defaults_are_each_providers_own(self):
        got = efforts("gemini:gemini-3.5-flash,cerebras,groq")

        self.assertEqual(
            got,
            {
                "gemini:gemini-3.5-flash": "none",
                "cerebras:gpt-oss-120b": "low",
                "groq:openai/gpt-oss-120b": "low",
            },
        )

    def test_a_geminis_none_in_a_chain_is_not_sent_to_gpt_oss(self):
        for value in ("none", "minimal"):
            with self.subTest(value=value):
                with self.assertLogs("adi.llm", level="WARNING") as logs:
                    got = efforts(
                        "gemini:gemini-3.5-flash,cerebras,groq",
                        RAG_ANSWERER_REASONING_EFFORT=value,
                    )

                self.assertEqual(got["gemini:gemini-3.5-flash"], value)
                self.assertEqual(got["cerebras:gpt-oss-120b"], "low")
                self.assertEqual(got["groq:openai/gpt-oss-120b"], "low")
                self.assertTrue(
                    any(r.getMessage() == "llm.effort_not_sent" for r in logs.records)
                )

    def test_a_value_every_provider_documents_reaches_all_of_them(self):
        got = efforts(
            "gemini:gemini-3.5-flash,cerebras,groq",
            RAG_ANSWERER_REASONING_EFFORT="high",
        )

        self.assertEqual(set(got.values()), {"high"})

    def test_a_value_typed_in_capitals_is_sent_in_the_lower_case_that_was_accepted(
        self,
    ):
        """The guard compares lower-cased. What is SENT must be that form, or a
        provider whose enum is case-sensitive answers 400 to every question."""
        for typed, sent in (("Low", "low"), ("HIGH", "high")):
            with self.subTest(typed=typed):
                got = efforts(
                    "gemini:gemini-3.5-flash,groq", RAG_ANSWERER_REASONING_EFFORT=typed
                )

                self.assertEqual(set(got.values()), {sent})

        with self.assertLogs("adi.llm", level="WARNING"):
            got = efforts(
                "gemini:gemini-3.5-flash,groq", RAG_ANSWERER_REASONING_EFFORT="None"
            )
        self.assertEqual(got["gemini:gemini-3.5-flash"], "none")
        self.assertEqual(got["groq:openai/gpt-oss-120b"], "low")

    def test_a_single_groq_ignores_a_value_it_does_not_document(self):
        with self.assertLogs("adi.llm", level="WARNING"):
            got = efforts(
                "", RAG_ANSWERER_PROVIDER="groq", RAG_ANSWERER_REASONING_EFFORT="none"
            )

        self.assertEqual(list(got.values()), ["low"])

    def test_a_single_groq_takes_one_it_does(self):
        got = efforts(
            "", RAG_ANSWERER_PROVIDER="groq", RAG_ANSWERER_REASONING_EFFORT="medium"
        )

        self.assertEqual(list(got.values()), ["medium"])

    def test_a_provider_that_documents_no_list_still_takes_the_override(self):
        got = efforts(
            "", RAG_ANSWERER_PROVIDER="mistral", RAG_ANSWERER_REASONING_EFFORT="low"
        )

        self.assertEqual(list(got.values()), ["low"])

    def test_a_model_of_another_family_is_sent_none_of_gpt_oss_s_extras(self):
        for chain in ("groq:llama-3.3-70b-versatile", "cerebras:llama3.1-8b"):
            with self.subTest(chain=chain):
                links = build(chain, RAG_ANSWERER_REASONING_EFFORT="high").links

                self.assertEqual(dict(links[0].request_extras), {})

    def test_a_pinned_single_provider_model_of_another_family_too(self):
        links = build(
            "",
            RAG_ANSWERER_PROVIDER="groq",
            RAG_ANSWERER_MODEL="llama-3.3-70b-versatile",
            RAG_ANSWERER_BASE_URL="",
            # Not even an operator's own override reaches a model that does
            # not take the parameter.
            RAG_ANSWERER_REASONING_EFFORT="high",
        ).links

        self.assertEqual(dict(links[0].request_extras), {})
        # Its output ceiling is Groq's per-minute window, not the model's.
        self.assertEqual(links[0].token_cap(4096), 1500)

    def test_the_other_gpt_oss_sizes_keep_them(self):
        links = build("groq:openai/gpt-oss-20b").links

        self.assertEqual(dict(links[0].request_extras), {"reasoning_effort": "low"})

    def test_default_still_turns_the_extra_off_everywhere(self):
        got = efforts(
            "gemini:gemini-3.5-flash,cerebras,groq",
            RAG_ANSWERER_REASONING_EFFORT="default",
        )

        self.assertEqual(set(got.values()), {None})
