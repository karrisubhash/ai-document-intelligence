"""Answer-quality fixes: false abstentions, and what reaches the model.

Each class pins one way a correct answer used to be lost, and the guard
that keeps the fix from turning into an invented one.
"""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from documents import rag
from documents.rag import engine, followup
from documents.rag.llm import ABSTENTION_TEXT, declines_to_answer
from documents.tests_providers import (
    PASSAGE,
    FakeSearchResponse,
    StubChatClient,
    answerer_with,
    completion,
)
from documents.tests_rag import chunk, hits

# --- A refusal in the model's own words is a refusal ------------------------


class DeclinesToAnswerTests(SimpleTestCase):
    def test_a_refusal_in_the_models_own_words_is_recognised(self):
        for reply in (
            "I couldn't find the PhD fee in these documents. They only list B.Tech.",
            "I could not find any information about remote work.",
            "The documents do not mention a hostel curfew.",
            "There is no information about that in the provided passages.",
            "That is not covered in the documents provided.",
            "I couldn\N{RIGHT SINGLE QUOTATION MARK}t find that.",
        ):
            with self.subTest(reply=reply):
                self.assertTrue(declines_to_answer(reply))

    def test_an_answer_is_never_taken_for_a_refusal(self):
        """Anything that cites a passage or states a figure is an answer -
        possibly a wrong one - and must still go through grounding."""
        for reply in (
            "The minimum attendance is 75 percent [1].",
            "I couldn't find enough information about fees, but [1] says so.",
            "The documents do not mention a late fee, but the fine is 50 rupees.",
            "Students must attend labs.",
            "",
            "   ",
        ):
            with self.subTest(reply=reply):
                self.assertFalse(declines_to_answer(reply))


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class RefusalReachesTheEngineAsOneTests(TestCase):
    """The reason matters downstream: MODEL_ABSTAINED may be replaced by chat's
    general reply, UNGROUNDED (the hallucination signal) may not."""

    def ask(self, reply):
        answerer = answerer_with(StubChatClient(completion(reply)))
        found = hits((chunk(1, PASSAGE), 1.0))
        with (
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
        ):
            return rag.ask(None, "What is the minimum attendance for the examination?")

    def test_a_refusal_in_other_words_is_model_abstained_not_ungrounded(self):
        answer = self.ask(
            "I couldn't find the PhD attendance rule in these documents. "
            "They only describe undergraduate courses."
        )

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)
        self.assertEqual(answer.answer, ABSTENTION_TEXT)

    def test_an_uncited_claim_is_still_ungrounded(self):
        answer = self.ask("Attendance is mandatory for everyone.")

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_fabricated_figure_is_still_ungrounded(self):
        answer = self.ask("The minimum attendance is 80 percent [1].")

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)


# --- "Is it mandatory...?" is not a follow-up -------------------------------


class ExpletiveItTests(SimpleTestCase):
    def test_a_grammatical_it_is_not_a_reference(self):
        """Read as a pronoun it made these questions follow-ups, and the
        previous turn's words were appended to the retrieval query."""
        for question in (
            "Is it mandatory to attend labs?",
            "Is it possible to defer an exam?",
            "Are they allowed to retake it?".replace("they", "students"),
            "Would it be necessary to submit a medical certificate?",
        ):
            with self.subTest(question=question):
                dependent, _ = followup.looks_dependent(question)
                self.assertFalse(dependent, question)

    def test_a_real_reference_still_is(self):
        for question in (
            "Is it included in the fee?",
            "Does it apply to part-time students?",
            "When is it due?",
            "Can they appeal?",
        ):
            with self.subTest(question=question):
                dependent, _ = followup.looks_dependent(question)
                self.assertTrue(dependent, question)
