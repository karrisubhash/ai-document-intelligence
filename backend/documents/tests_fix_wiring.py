"""The user's own words reach the two checks that need them.

The third review found that a correct answer is rejected when it repeats a
figure the user typed ("I have 70 percent attendance ..." answered with "At 70
percent attendance you ..."), and that a refusal repeating the year or clause
the user asked about was filed as an ungrounded answer. grounding.validate
learned `user_text` and llm.declines_to_answer learned `question`; those fixes
do nothing unless the engine and both answerers actually hand them over, which
is what these pin.
"""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from documents import rag
from documents.rag import context as ctx
from documents.rag import engine
from documents.rag.claude import ClaudeAnswerer
from documents.tests_claude_answerer import StubMessage, build_context, stub_client
from documents.tests_fix_engine import EngineHarness, Scripted, client_answerer
from documents.tests_rag_quality import TUITION, ranked

TYPED = "I have 70 percent attendance. What is the tuition per year for the programme?"


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class TheUsersFigureThroughTheEngineTests(EngineHarness, TestCase):
    def test_an_answer_may_repeat_a_figure_the_user_typed(self):
        client = Scripted("At 70 percent attendance the tuition is 85000 per year [1].")

        answer = self.ask(client_answerer(client), TUITION, question=TYPED)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        # Accepted at once: no corrective retry was spent on it.
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([c["marker"] for c in answer.citations], [1])

    def test_the_same_answer_is_rejected_when_the_user_did_not_type_it(self):
        client = Scripted("At 70 percent attendance the tuition is 85000 per year [1].")

        answer = self.ask(
            client_answerer(client),
            TUITION,
            question="What is the tuition per year for the programme?",
        )

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_figure_nobody_supplied_is_still_rejected(self):
        client = Scripted("At 80 percent attendance the tuition is 85000 per year [1].")

        answer = self.ask(client_answerer(client), TUITION, question=TYPED)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_figure_the_user_typed_is_not_a_source(self):
        """Echoing 70 is allowed; stating that the passage says 70 is not made
        true by it, and the figure that matters (the tuition) is still checked."""
        client = Scripted("At 70 percent attendance the tuition is 99999 per year [1].")

        answer = self.ask(client_answerer(client), TUITION, question=TYPED)

        self.assertTrue(answer.abstained)
        self.assertTrue(
            any("99999" in failure for failure in answer.grounding_failures),
            answer.grounding_failures,
        )

    def test_a_follow_up_may_repeat_a_figure_from_the_users_own_words(self):
        """On a widened follow-up the user's own words travel as `focus`."""
        client = Scripted("At 70 percent attendance the tuition is 85000 per year [1].")
        real_ask = rag.ask

        def with_focus(user, question, **options):
            return real_ask(
                user, question, focus="I have 70 percent attendance", **options
            )

        with mock.patch.object(rag, "ask", side_effect=with_focus):
            answer = self.ask(
                client_answerer(client),
                TUITION,
                question="What is the tuition per year for the programme?",
            )

        self.assertFalse(answer.abstained, answer.grounding_failures)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class TheRefusalDetectorGetsTheQuestionTests(EngineHarness, TestCase):
    QUESTION = "What is the tuition fee for 2023-24?"

    def test_a_refusal_that_repeats_the_asked_year_is_a_refusal(self):
        client = Scripted("I couldn't find the 2023-24 fee in these documents.")

        answer = self.ask(client_answerer(client), TUITION, question=self.QUESTION)

        self.assertTrue(answer.abstained)
        # Not filed as an ungrounded answer, the hallucination signal, and so
        # chat's general reply is not held back by it.
        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)

    def test_a_refusal_with_a_figure_nobody_typed_is_not(self):
        client = Scripted("I couldn't find the fee, but it is probably 95000 per year.")

        answer = self.ask(client_answerer(client), TUITION, question=self.QUESTION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_the_claude_answerer_is_given_the_question_too(self):
        answerer = ClaudeAnswerer()
        context = build_context()
        answerer._client = stub_client(
            StubMessage("I couldn't find the 2023-24 fee in these documents.")
        )

        response = answerer.answer(
            question=self.QUESTION,
            context=context,
            prompt=ctx.render(context, self.QUESTION),
        )

        self.assertTrue(response.abstained)


class TheContextCarriesWhatTheUserTypedTests(SimpleTestCase):
    def test_leaving_out_the_judged_passages_keeps_it(self):
        items = ranked(
            ("Passage one has enough text to count as a passage.", 0.9),
            ("Passage two also has enough text to count as one.", 0.8),
        )
        items[1].signals["model_judged"] = True
        built = ctx.build(items, semantic_scores=True)
        built.user_text = "I have 70 percent attendance"

        vetted = built.without_model_judged()

        self.assertEqual(len(vetted.passages), 1)
        self.assertEqual(vetted.user_text, "I have 70 percent attendance")
