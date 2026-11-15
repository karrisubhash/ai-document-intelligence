"""Answer-quality fixes: false abstentions, and what reaches the model.

Each class pins one way a correct answer used to be lost, and the guard
that keeps the fix from turning into an invented one.
"""

import contextlib
from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from documents import agents, rag
from documents.agents import runner
from documents.models import Document, ProcessingJob
from documents.observability import metrics
from documents.processing.pipeline import run_pipeline
from documents.rag import context as ctx
from documents.rag import engine, facts, followup, grounding, judging
from documents.rag import gateway as gateway_module
from documents.rag.gateway import Gateway, Plan
from documents.rag.llm import ABSTENTION_TEXT, ExtractiveAnswerer, declines_to_answer
from documents.rag.reranking import RerankedChunk
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user
from documents.tests_llm_gateway import (
    GatewayTestCase,
    ModelClient,
    StatusError,
    build,
    wire,
)
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
            "Is it possible for students to defer an exam?",
            "Is it allowed to bring guests into the library?",
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
            # An adjective alone does not make "it" a placeholder: what it is
            # about must follow ("to register", "that ...").
            "Is it allowed on weekends?",
            "Is it required for part-time students?",
            "Is it permitted for hostellers?",
            "Is it fine for first year students?",
            "Is it mandatory?",
        ):
            with self.subTest(question=question):
                dependent, _ = followup.looks_dependent(question)
                self.assertTrue(dependent, question)


# --- A generative model judges relevance, not the score floor ---------------

# A correct passage the small cross-encoder scored far below its floor: the
# question is a paraphrase that shares no words with it.
PARAPHRASED = (
    "Students are required to be present for three quarters of all scheduled "
    "classes before they may sit the end-semester examination."
)
PARAPHRASE_QUESTION = "How often do I have to show up to be allowed to sit exams?"
FLOOR = 0.30


class LowScoreReranker:
    """A reranker whose scores sit under its own floor, as MiniLM's do for a
    paraphrase: it ranks the answer first and scores it 0.02."""

    name = "fake-cross-encoder"
    version = "1"
    semantic = True
    semantic_scores = True
    default_relevance_floor = FLOOR

    def __init__(self, scores=None):
        self.scores = scores or {}

    def rerank(self, query, candidates):
        ranked = [
            RerankedChunk(
                chunk=candidate.chunk,
                score=self.scores.get(candidate.chunk.id, 0.02),
                retrieval_score=candidate.score,
                signals={},
            )
            for candidate in candidates
        ]
        ranked.sort(key=lambda item: (-item.score, item.chunk.id))
        return ranked


def ranked(*pairs):
    """RerankedChunk items from (text, score), best first, ids from 1."""
    return [
        RerankedChunk(chunk=chunk(index, text), score=score, signals={})
        for index, (text, score) in enumerate(pairs, start=1)
    ]


class FakeGateway(Gateway):
    """A gateway that only says whether a model will be tried."""

    def __init__(self, available):
        self._available = available

    def plan(self, purpose, **kwargs):
        return Plan([object()] if self._available else [])

    def available(self, purpose):
        return self._available


class AdmissionTests(SimpleTestCase):
    def test_the_reranker_top_and_retrieval_top_are_admitted_in_reranked_order(self):
        items = ranked(
            *[(f"Passage number {n} has enough text to count.", 0.05) for n in range(8)]
        )
        # Retrieval put the last one first.
        retrieval_ranks = {item.chunk.id: n + 1 for n, item in enumerate(items[::-1])}

        admitted = judging.admit(items, [], retrieval_ranks, minimum=3, retrieval_top=1)

        self.assertEqual(
            [item.chunk.id for item in admitted],
            [1, 2, 3, 8],
        )
        self.assertTrue(all(item.signals["model_judged"] for item in admitted))
        self.assertFalse(items[4].signals)

    def test_passages_already_above_the_floor_use_up_places_and_are_not_flagged(self):
        items = ranked(
            ("The answer is in this passage of text here.", 0.9),
            ("A second reasonably long passage of text.", 0.04),
            ("A third reasonably long passage of text.", 0.03),
        )

        admitted = judging.admit(items, items[:1], {}, minimum=2, retrieval_top=0)

        self.assertEqual([item.chunk.id for item in admitted], [2])
        self.assertNotIn("model_judged", items[0].signals)

    def test_a_fragment_or_a_page_number_is_never_admitted(self):
        items = ranked(
            ("Page 4", 0.05), ("1234 5678 9012 3456 7890 1234 5678 90.", 0.05)
        )

        self.assertEqual(judging.admit(items, [], {}, minimum=5, retrieval_top=5), [])

    def test_only_a_generative_model_with_capacity_judges(self):
        generative = mock.Mock(generative=True, gateway=FakeGateway(True))
        self.assertTrue(judging.model_can_judge(generative))

        out_of_quota = mock.Mock(generative=True, gateway=FakeGateway(False))
        self.assertFalse(judging.model_can_judge(out_of_quota))

        self.assertFalse(judging.model_can_judge(ExtractiveAnswerer()))
        # A test double's attributes are all truthy; that is not a model.
        self.assertFalse(judging.model_can_judge(mock.Mock()))

    @override_settings(RAG_MODEL_JUDGES_RELEVANCE=False)
    def test_the_switch_restores_the_floor(self):
        generative = mock.Mock(generative=True, gateway=FakeGateway(True))
        self.assertFalse(judging.model_can_judge(generative))


class ModelJudgesRelevanceTests(TestCase):
    """The engine hands a model what the floor would have hidden."""

    def ask(self, answerer, text=PARAPHRASED, scores=None, figure_hints=None):
        found = hits((chunk(1, text), 1.0))
        # Like the real function, which has nothing to say about no documents.
        hint_patch = mock.patch.object(
            engine,
            "_figure_hints",
            side_effect=lambda ids: list(figure_hints or []) if ids else [],
        )
        with (
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(
                engine, "get_reranker", return_value=LowScoreReranker(scores)
            ),
            hint_patch as hints_called,
        ):
            answer = rag.ask(None, PARAPHRASE_QUESTION)
        return answer, hints_called

    def generative(self, reply):
        # The real gateway and the stub client: its plan has a link to try.
        return answerer_with(StubChatClient(completion(reply)))

    def test_a_correct_paraphrase_below_the_floor_is_answered_by_the_model(self):
        answer, _ = self.ask(
            self.generative("You need to attend three quarters of classes [1].")
        )

        self.assertFalse(answer.abstained, answer.notes)
        self.assertTrue(answer.generative)
        self.assertEqual([c["marker"] for c in answer.citations], [1])
        self.assertTrue(
            any("passed to the generative model to judge" in n for n in answer.notes),
            answer.notes,
        )

    def test_without_a_model_the_floor_still_applies(self):
        """A quote cannot tell that a passage is beside the point."""
        answer, _ = self.ask(ExtractiveAnswerer())

        self.assertTrue(answer.abstained)
        self.assertEqual(
            answer.abstention_reason, engine.Abstention.NO_RELEVANT_PASSAGES
        )

    def test_a_model_that_finds_nothing_is_an_ordinary_model_abstention(self):
        answer, hints_called = self.ask(
            self.generative("I couldn't find that in these documents."),
            figure_hints=["'Scan' has 1 image(s) on page 1"],
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.MODEL_ABSTAINED)
        # The passages a model was only asked to judge are not evidence of
        # where an answer is, so they name no "unreadable figure" either.
        hints_called.assert_called_once_with([])

    def test_a_wrong_figure_from_a_judged_passage_is_still_caught(self):
        answer, _ = self.ask(
            self.generative("You must attend 80 percent of classes [1].")
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_passages_above_the_floor_are_unchanged(self):
        answer, hints_called = self.ask(
            self.generative("You must attend three quarters of classes [1]."),
            scores={1: 0.9},
        )

        self.assertFalse(answer.abstained)
        self.assertFalse(
            any("to judge" in note for note in answer.notes),
            "nothing was below the floor, so nothing was admitted",
        )


class QuotingNeverSeesJudgedPassagesTests(SimpleTestCase):
    KEPT = PASSAGE
    JUDGED = "The minimum attendance for hostel residents at evening roll call is 90 percent."
    QUESTION = "What is the minimum attendance?"

    def context(self):
        items = ranked((self.KEPT, 0.9), (self.JUDGED, 0.02))
        items[1].signals["model_judged"] = True
        return ctx.build(items, semantic_scores=True)

    def test_the_context_view_for_quoting_leaves_them_out(self):
        built = self.context()

        self.assertEqual([p.model_judged for p in built.passages], [False, True])
        quoting = built.without_model_judged()
        self.assertEqual([p.marker for p in quoting.passages], [1])
        # Nothing to remove: the same object, not a copy.
        self.assertIs(quoting.without_model_judged(), quoting)

    def test_a_quote_after_a_provider_failure_uses_only_the_vouched_passage(self):
        answerer = answerer_with(StubChatClient(raises=TimeoutError("timed out")))

        response = answerer.answer(
            question=self.QUESTION, context=self.context(), prompt="PROMPT"
        )

        self.assertTrue(response.metadata["fell_back_from"])
        self.assertIn("75", response.text)
        self.assertNotIn("90", response.text)

    def test_the_provider_failure_with_only_judged_passages_quotes_nothing(self):
        items = ranked((self.JUDGED, 0.02))
        items[0].signals["model_judged"] = True
        built = ctx.build(items, semantic_scores=True)
        answerer = answerer_with(StubChatClient(raises=TimeoutError("timed out")))

        response = answerer.answer(
            question=self.QUESTION, context=built, prompt="PROMPT"
        )

        self.assertTrue(response.abstained)
        self.assertNotIn("90", response.text)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER="documents.processing.embedding.hashing.HashingEmbeddingProvider",
)
class LeadingChunksTests(TestCase):
    """ "Summarise this document" matches nothing in particular, and the answer
    is the start of it."""

    def setUp(self):
        self.owner = make_user("lead_owner", department="Registry")
        self.outsider = make_user("lead_outsider", department="Physics")
        body = "\n".join(
            f"Section {n}. " + ("The registry keeps records of enrolment. " * 12)
            for n in range(1, 9)
        ).encode()
        self.document = Document.objects.create(
            title="Registry Handbook",
            document_type="REGULATION",
            owner=self.owner,
            department="Registry",
        )
        version = self.document.versions.create(
            version_number=1,
            file=ContentFile(body, name="handbook.txt"),
            file_name="handbook.txt",
            file_size=len(body),
            uploaded_by=self.owner,
        )
        self.document.current_version = version
        self.document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def test_the_opening_chunks_come_back_in_reading_order(self):
        leading = judging.leading_chunks(self.owner, self.document.id, set())

        self.assertEqual(len(leading), judging.LEADING_CHUNKS)
        indexes = [item.chunk.chunk_index for item in leading]
        self.assertEqual(indexes, sorted(indexes))
        self.assertEqual(indexes[0], 0)
        self.assertTrue(all(item.signals["model_judged"] for item in leading))

    def test_chunks_already_taken_are_not_repeated(self):
        first = judging.leading_chunks(self.owner, self.document.id, set())
        taken = {item.chunk.id for item in first}

        again = judging.leading_chunks(self.owner, self.document.id, taken)

        self.assertFalse(taken & {item.chunk.id for item in again})

    def test_someone_who_cannot_read_the_document_gets_nothing(self):
        self.assertEqual(
            judging.leading_chunks(self.outsider, self.document.id, set()), []
        )

    def test_no_document_scope_means_no_leading_chunks(self):
        self.assertEqual(judging.leading_chunks(self.owner, None, set()), [])


# --- A model is given the compound question whole ---------------------------

THREE_PART = (
    "What is the attendance minimum, how many books can a student borrow, "
    "and what is the hostel curfew?"
)


class PlanningTests(TestCase):
    """Planning cuts a compound question up so a quoting answerer can answer
    each piece. A model reads it whole - and a cut-up question spends a
    free tier's few daily calls and loses what each part referred to."""

    def asked(self, answerer):
        calls = []

        def ask(user, question, **kwargs):
            calls.append(question)
            return rag.RagAnswer(
                question=question, answer=ABSTENTION_TEXT, abstained=True
            )

        with (
            mock.patch.object(runner, "ask", side_effect=ask),
            mock.patch.object(runner, "get_answerer", return_value=answerer),
        ):
            planned = agents.answer(None, THREE_PART)
        return calls, planned

    def generative(self):
        return mock.Mock(generative=True, gateway=FakeGateway(True))

    def test_a_model_gets_the_whole_question_in_one_call(self):
        calls, planned = self.asked(self.generative())

        self.assertEqual(calls, [THREE_PART])
        self.assertFalse(planned.planned)

    def test_a_quoting_answerer_still_gets_it_in_parts(self):
        calls, planned = self.asked(ExtractiveAnswerer())

        self.assertEqual(len(calls), 3)
        self.assertTrue(planned.planned)

    def test_a_model_with_no_capacity_left_is_planned_for_like_a_quote(self):
        out_of_quota = mock.Mock(generative=True, gateway=FakeGateway(False))

        calls, planned = self.asked(out_of_quota)

        self.assertEqual(len(calls), 3)

    @override_settings(RAG_PLAN_WHEN_GENERATIVE=True)
    def test_the_setting_brings_planning_back_for_a_model(self):
        calls, planned = self.asked(self.generative())

        self.assertEqual(len(calls), 3)


# --- The same fact, written two ways ----------------------------------------


class FactFormsTests(SimpleTestCase):
    def test_a_date_is_read_whole_however_it_is_written(self):
        for written in (
            "31.03.2025",
            "31/03/2025",
            "31-03-2025",
            "31 March 2025",
            "31st March 2025",
            "31 Mar 2025",
            "March 31, 2025",
            "2025-03-31",
        ):
            with self.subTest(written=written):
                remainder, dates = facts.extract_dates(f"Pay by {written}.")
                self.assertEqual(dates, {"date:2025-03-31"})
                self.assertNotIn("2025", remainder)

    def test_a_date_that_can_be_read_two_ways_is_left_alone(self):
        """04.03.2025 is the 4th of March or the 3rd of April; accepting either
        reading would accept a wrong date."""
        remainder, dates = facts.extract_dates("On 04.03.2025 and on 5 May 2025.")

        self.assertEqual(dates, {"date:2025-05-05"})
        self.assertIn("04.03.2025", remainder)

    def test_a_date_that_does_not_exist_is_one_exact_token(self):
        """Read as a day and a year it was satisfied by any 31 and any 2025."""
        remainder, dates = facts.extract_dates("On 31 February 2025 and 31.02.2025.")

        self.assertEqual(dates, {"date:?31february2025"})
        # The numeric spelling is left in the text, where it is one token.
        self.assertIn("31.02.2025", remainder)

    def test_a_day_and_month_with_no_year_is_a_date(self):
        for written in (
            "due on 15 September",
            "due on September 15",
            "15th of September",
        ):
            with self.subTest(written=written):
                self.assertEqual(facts.extract_dates(written)[1], {"date:--09-15"})

        # A day that month does not have is one exact token, not a day.
        self.assertEqual(facts.extract_dates("by 30 February")[1], {"date:?30february"})

    def test_may_is_a_verb_not_a_month_when_there_is_no_year(self):
        self.assertEqual(facts.extract_dates("Section 15 may apply")[1], set())
        self.assertEqual(facts.extract_dates("on 15 May 2025")[1], {"date:2025-05-15"})

    def test_a_full_date_also_stands_for_its_day_and_month(self):
        self.assertEqual(
            facts.year_less({"date:2025-08-15", "date:?31april2025", "2025"}),
            {"date:--08-15"},
        )

    def test_a_year_range_is_one_range(self):
        self.assertEqual(facts.canonical_range("2024-25"), "2024-2025")
        self.assertEqual(facts.canonical_range("1999-00"), "1999-2000")
        # Not a range of consecutive years: left as it is.
        self.assertEqual(facts.canonical_range("2024-05"), "2024-05")
        self.assertEqual(facts.canonical_range("2024-2025"), "2024-2025")
        self.assertEqual(facts.prepare("2024\N{EN DASH}25"), "2024-25")
        self.assertEqual(facts.range_years("2024-2025"), {"2024", "2025"})
        # A range of amounts is not a pair of years.
        self.assertEqual(facts.range_years("1000-2000"), set())
        self.assertEqual(facts.range_years("2019-2021"), set())
        self.assertEqual(facts.range_parts("2019-2021"), ("2019", "2021"))
        self.assertIsNone(facts.range_parts("2019"))

    def test_number_words_stand_for_numerals_when_they_count_something(self):
        self.assertEqual(
            facts.number_words(
                "within seven days, twenty-five rupees, ten working days"
            ),
            {"7", "25", "10"},
        )
        self.assertEqual(facts.number_words("one week and twenty members"), {"1", "20"})
        self.assertEqual(facts.number_words("a statement of the situation"), set())

    def test_a_number_word_that_counts_nothing_is_not_a_figure(self):
        """Any passage with "one" in it supported any answer that said 1."""
        for text in (
            "No one may attend the examination without the card.",
            "Classes in the second semester begin in January.",
            "The third time a student is late, a warning is issued.",
            "One of the conditions is attendance.",
            "Zero tolerance applies.",
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.number_words(text), set())

    def test_a_month_the_table_lacks_is_not_an_error(self):
        """The pattern accepted "Sepember" and the table had no such key: a
        KeyError in grounding is a failed request."""
        for text in (
            "The deadline is 15 Sepember 2024.",
            "Sepember 15, 2024",
            "15 " + chr(0x17F) + "eptember 2024",  # a long s, which re.I matched
            "15 " + chr(0x131) + "an 2024",  # a dotless i
        ):
            with self.subTest(text=text):
                remainder, dates = facts.extract_dates(text)
                self.assertEqual(dates, set())
                self.assertEqual(remainder, text)

    def test_formal_and_ranged_dates_are_read(self):
        both = {"date:2025-03-15", "date:2025-03-20"}
        for written, expected in (
            ("on the 15th day of March, 2025", {"date:2025-03-15"}),
            ("from 15 to 20 March 2025", both),
            ("from 15-20 March 2025", both),
            ("from 15th until 20th March, 2025", both),
        ):
            with self.subTest(written=written):
                self.assertEqual(facts.extract_dates(written)[1], expected)

    def test_a_range_of_days_that_is_not_a_real_date_is_one_exact_token(self):
        remainder, dates = facts.extract_dates("from 25 to 35 March 2025")

        self.assertEqual(dates, {"date:?25to35march2025"})
        self.assertNotIn("35", remainder)

    def test_a_group_of_citations_is_split(self):
        self.assertEqual(
            facts.normalize_markers("Yes [1, 2]; also [3;4]."),
            "Yes [1][2]; also [3][4].",
        )
        self.assertEqual(
            facts.normalize_markers("Yes [1] and [2]."), "Yes [1] and [2]."
        )


def one_passage(text, title="Doc"):
    item = RerankedChunk(chunk=chunk(1, text, title=title), score=0.9, signals={})
    return ctx.build([item], semantic_scores=True)


def two_passages(first, second):
    items = [
        RerankedChunk(chunk=chunk(1, first), score=0.9, signals={}),
        RerankedChunk(chunk=chunk(2, second), score=0.8, signals={}),
    ]
    return ctx.build(items, semantic_scores=True)


class GroundingSpellingTests(SimpleTestCase):
    """A correct answer is not discarded for how it spells a fact - and a
    wrong one is still caught."""

    def check(self, answer, context):
        return grounding.validate(answer, context)

    def test_a_reformatted_date_is_the_same_date(self):
        context = one_passage("The last date for fee payment is 31.03.2025.")
        for answer in (
            "Pay by 31 March 2025 [1].",
            "The deadline is March 31, 2025 [1].",
            "Fees are due on 31st March 2025 [1].",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(self.check(answer, context).ok)

    def test_a_different_date_is_still_caught(self):
        context = one_passage("The last date for fee payment is 31 March 2025.")
        for answer in (
            "Pay by 31 May 2025 [1].",
            "Pay by 30 March 2025 [1].",
            "Pay by 31 March 2026 [1].",
        ):
            with self.subTest(answer=answer):
                report = self.check(answer, context)
                self.assertFalse(report.ok)
                self.assertTrue(report.unsupported_facts)

    def test_a_date_that_does_not_exist_is_not_supported_by_its_parts(self):
        context = one_passage("The last date is 31 March 2025.")

        self.assertTrue(self.check("It is 31 March 2025 [1].", context).ok)
        self.assertFalse(self.check("It is 31 April 2025 [1].", context).ok)
        self.assertFalse(self.check("It is 31 February 2025 [1].", context).ok)

    def test_a_date_that_does_not_exist_is_not_rescued_by_the_same_numbers_elsewhere(
        self,
    ):
        """Read as a day and a year, "31 April 2025" was supported by a "31" and
        a "2025" anywhere in the passages - here, a section and a year that have
        nothing to do with a date."""
        context = one_passage(
            "The last date is 31 March 2025. Section 31 of the rules was revised in 2025."
        )

        self.assertTrue(self.check("It is 31 March 2025 [1].", context).ok)
        self.assertFalse(self.check("It is 31 April 2025 [1].", context).ok)

    def test_an_answer_may_leave_the_year_out_but_not_change_the_day_or_month(self):
        context = one_passage("Fees are due on 15 August 2025.")

        self.assertTrue(self.check("Fees are due on 15 August [1].", context).ok)
        self.assertFalse(self.check("Fees are due on 16 August [1].", context).ok)
        self.assertFalse(self.check("Fees are due on 15 September [1].", context).ok)

    def test_an_ambiguous_numeric_date_must_match_as_written(self):
        context = one_passage("The form is due on 04.03.2025.")

        self.assertTrue(self.check("It is due on 04.03.2025 [1].", context).ok)
        self.assertFalse(self.check("It is due on 4 March 2025 [1].", context).ok)
        self.assertFalse(self.check("It is due on 3 April 2025 [1].", context).ok)

    def test_a_year_range_matches_however_it_is_written(self):
        context = one_passage("Fee Structure 2024-25. Tuition is 85000.")
        for answer in (
            "For 2024-2025 the tuition is 85000 [1].",
            "For 2024\N{EN DASH}25 the tuition is 85000 [1].",
            "For 2024-25 the tuition is 85000 [1].",
            "For 2024 to 2025 the tuition is 85000 [1].",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(self.check(answer, context).ok, answer)

        self.assertFalse(
            self.check("For 2025-26 the tuition is 85000 [1].", context).ok
        )
        self.assertFalse(
            self.check("For 2023-24 the tuition is 85000 [1].", context).ok
        )

    def test_the_title_the_reader_sees_supports_a_figure_in_it(self):
        context = one_passage("Tuition is 85000.", title="Fee Structure 2024-25")

        self.assertTrue(self.check("For 2024-25 the tuition is 85000 [1].", context).ok)
        self.assertFalse(
            self.check("For 2023-24 the tuition is 85000 [1].", context).ok
        )

    def test_a_number_word_in_the_passage_supports_the_numeral(self):
        context = one_passage("Appeals must be filed within seven days of the result.")

        self.assertTrue(self.check("File within 7 days [1].", context).ok)
        self.assertFalse(self.check("File within 8 days [1].", context).ok)

    def test_a_number_word_in_the_answer_is_not_a_figure(self):
        context = one_passage("Students may borrow four books at a time.")

        self.assertTrue(self.check("One of the rules is four books [1].", context).ok)

    def test_a_number_word_that_counts_nothing_supports_no_figure(self):
        """ "No one may attend" supported "1 day"; a hallucinated 1 passed in a
        quarter of the corpus's passages."""
        for passage, answer, figure in (
            (
                "No one may attend the examination without the card.",
                "The card is needed for 1 day [1].",
                "1",
            ),
            (
                "Classes in the second semester begin in January.",
                "There are 2 classes in January [1].",
                "2",
            ),
            (
                "The third time a student is late, a warning is issued.",
                "A warning is issued after 3 late arrivals [1].",
                "3",
            ),
        ):
            with self.subTest(answer=answer):
                report = self.check(answer, one_passage(passage))
                self.assertIn(figure, report.unsupported_facts)

    def test_a_title_supports_a_year_and_nothing_else(self):
        text = "Residents must keep their rooms clean."

        versioned = self.check(
            "Residents have 2 weeks to move out [1].",
            one_passage(text, title="Hostel Rules v2"),
        )
        coded = self.check(
            "The late fee is Rs 117 [1].",
            one_passage(text, title="Circular TR/2024/117"),
        )
        dated = self.check(
            "The rules were revised in 2019 [1].",
            one_passage(text, title="Fee Rules 2019"),
        )

        self.assertIn("2", versioned.unsupported_facts)
        self.assertIn("117", coded.unsupported_facts)
        self.assertNotIn("2019", dated.unsupported_facts)

    def test_a_formal_date_is_the_same_date(self):
        context = one_passage(
            "Fees are due on the 15th day of March, 2025 without exception."
        )

        self.assertTrue(self.check("Fees are due on 15 March 2025 [1].", context).ok)
        self.assertFalse(self.check("Fees are due on 16 March 2025 [1].", context).ok)

    def test_a_range_of_days_supports_each_whole_date(self):
        context = one_passage(
            "The camp runs from 15 to 20 March 2025 for all students."
        )

        self.assertTrue(
            self.check(
                "The camp runs from 15 March 2025 to 20 March 2025 [1].", context
            ).ok
        )
        self.assertFalse(
            self.check(
                "The camp runs from 16 March 2025 to 20 March 2025 [1].", context
            ).ok
        )

    def test_a_dashed_range_is_supported_by_its_two_ends(self):
        context = one_passage("The course ran from 2019 to 2021 in two phases.")

        self.assertTrue(
            self.check("The course ran in 2019" + chr(0x2013) + "2021 [1].", context).ok
        )
        self.assertTrue(self.check("The course ran in 2019-2021 [1].", context).ok)
        self.assertFalse(self.check("The course ran in 2019-2022 [1].", context).ok)

    def test_a_range_of_amounts_does_not_support_one_end_alone(self):
        context = one_passage(
            "Fees range from Rs 1000-2000 depending on the programme."
        )

        report = self.check("The fee is Rs 2000 [1].", context)

        self.assertIn("2000", report.unsupported_facts)


class CitationFormTests(TestCase):
    PASSAGES = ("Attendance must be 75 percent.", "Condonation needs 65 percent.")

    def validate(self, answer):
        return grounding.validate(answer, two_passages(*self.PASSAGES))

    def test_a_group_of_citations_is_two_citations(self):
        report = self.validate(
            "Attendance is 75 percent and 65 percent with leave [1, 2]."
        )

        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.cited_markers, (1, 2))
        self.assertIn("[1][2]", report.text)

    def test_a_marker_one_past_the_last_passage_is_a_fabricated_source(self):
        """It was forgiven when it stood beside a real one, and a whole
        unsourced sentence went through with its marker quietly deleted."""
        report = self.validate("Attendance must be 75 percent [1] [3].")

        self.assertFalse(report.ok)
        self.assertEqual(report.invalid_markers, (3,))
        self.assertIn("[3]", report.text)

    def test_a_marker_zero_is_a_fabricated_source(self):
        self.assertFalse(self.validate("Attendance must be 75 percent [0][1].").ok)

    def test_an_unsourced_sentence_cannot_hide_behind_a_missing_marker(self):
        report = self.validate(
            "Attendance must be 75 percent [1]. "
            "Refunds are always given in full within a week of the request [3]."
        )

        self.assertFalse(report.ok)

    def test_a_documents_own_grouped_reference_is_not_a_citation(self):
        """The document says "[2, 12]"; reading it as two citations rejected a
        verbatim, correct quote."""
        context = one_passage(
            "The committee reviewed earlier studies [2, 12] of the programme."
        )

        report = grounding.validate(
            "The committee reviewed earlier studies [2, 12] of the programme. [1]",
            context,
        )

        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.cited_markers, (1,))

    def test_the_quoting_answerer_neutralises_a_documents_grouped_reference(self):
        built = one_passage(
            "The committee reviewed earlier studies [2, 12] of the programme."
        )

        response = ExtractiveAnswerer().answer(
            question="What did the committee review?", context=built, prompt=""
        )

        self.assertIn("(2, 12)", response.text)
        self.assertNotIn("[2", response.text)
        self.assertTrue(grounding.validate(response.text, built).ok)

    def test_a_fabricated_source_still_fails(self):
        """The cheapest hallucination signal there is."""
        for answer in (
            "Attendance must be 75 percent [1] [4].",  # two past the end
            "Attendance must be 75 percent [7].",  # nothing else resolves
            "Attendance must be 75 percent [3].",  # the stray, but alone
        ):
            with self.subTest(answer=answer):
                self.assertFalse(self.validate(answer).ok)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class EngineShowsTheCheckedAnswerTests(TestCase):
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

    def test_a_stray_marker_is_a_fabricated_source_not_a_typo(self):
        answer = self.ask("You need 75 percent attendance [1] [2].")

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_grouped_citation_resolves(self):
        answer = self.ask("You need 75 percent attendance [1, 1].")

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual([c["marker"] for c in answer.citations], [1])


# --- Chunks beside the best ones, and more room for a model -----------------


def index_handbook(owner):
    """A document of several chunks, each line numbered so a chunk's place is
    readable from its text."""
    body = "\n".join(
        f"Section {n}. " + ("The registry keeps records of enrolment. " * 12)
        for n in range(1, 9)
    ).encode()
    document = Document.objects.create(
        title="Registry Handbook",
        document_type="REGULATION",
        owner=owner,
        department="Registry",
    )
    version = document.versions.create(
        version_number=1,
        file=ContentFile(body, name="handbook.txt"),
        file_name="handbook.txt",
        file_size=len(body),
        uploaded_by=owner,
    )
    document.current_version = version
    document.save(update_fields=["current_version"])
    run_pipeline(ProcessingJob.objects.create(document_version=version).id)
    return document


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER="documents.processing.embedding.hashing.HashingEmbeddingProvider",
)
class NeighbourTests(TestCase):
    def setUp(self):
        self.owner = make_user("near_owner", department="Registry")
        self.outsider = make_user("near_outsider", department="Physics")
        self.document = index_handbook(self.owner)
        version = self.document.current_version
        version.refresh_from_db()
        self.chunks = list(
            version.chunks.filter(document_content=version.current_content).order_by(
                "chunk_index"
            )
        )
        self.assertGreaterEqual(len(self.chunks), 5)

    def anchor(self, index):
        return RerankedChunk(chunk=self.chunks[index], score=0.9, signals={})

    def test_the_chunks_either_side_come_back_flagged_for_a_model(self):
        beside = judging.neighbours(self.owner, [self.anchor(3)], set())

        self.assertEqual(
            [(i.chunk.chunk_index, i.signals["side"]) for i in beside],
            [(2, "before"), (4, "after")],
        )
        self.assertTrue(all(i.signals["model_judged"] for i in beside))
        self.assertTrue(
            all(i.signals["neighbour_of"] == self.chunks[3].id for i in beside)
        )

    def test_the_first_chunk_has_nothing_before_it(self):
        beside = judging.neighbours(self.owner, [self.anchor(0)], set())

        self.assertEqual([i.signals["side"] for i in beside], ["after"])

    def test_chunks_already_in_the_context_are_not_repeated(self):
        taken = {self.chunks[4].id}

        beside = judging.neighbours(self.owner, [self.anchor(3)], taken)

        self.assertEqual([i.chunk.chunk_index for i in beside], [2])

    def test_only_the_top_few_anchors_get_neighbours(self):
        anchors = [self.anchor(1), self.anchor(3)]

        beside = judging.neighbours(self.owner, anchors, set(), limit=1)

        self.assertEqual(
            {i.signals["neighbour_of"] for i in beside}, {self.chunks[1].id}
        )

    def test_someone_who_cannot_read_the_document_gets_no_neighbours(self):
        self.assertEqual(judging.neighbours(self.outsider, [self.anchor(3)], set()), [])

    def test_a_chunk_that_is_not_a_database_row_has_none(self):
        self.assertEqual(
            judging.neighbours(
                self.owner, ranked(("A fake passage of enough text.", 0.9)), set()
            ),
            [],
        )

    def test_the_prompt_says_which_passage_a_neighbour_continues(self):
        beside = judging.neighbours(self.owner, [self.anchor(3)], set())
        built = ctx.build([self.anchor(3), *beside], semantic_scores=True)

        prompt = ctx.render(built, "What does the registry keep?")

        self.assertIn(
            "the text directly before passage [1] in the same document", prompt
        )
        self.assertIn(
            "the text directly after passage [1] in the same document", prompt
        )
        self.assertEqual([p.adjacent_marker for p in built.passages], [0, 1, 1])


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER="documents.processing.embedding.hashing.HashingEmbeddingProvider",
    RAG_ANSWERER_PROVIDER="groq",
)
class ModelContextTests(TestCase):
    """What a model is shown, as opposed to what a quote is."""

    def setUp(self):
        self.owner = make_user("ctx_owner", department="Registry")
        self.document = index_handbook(self.owner)
        version = self.document.current_version
        version.refresh_from_db()
        self.chunks = list(
            version.chunks.filter(document_content=version.current_content).order_by(
                "chunk_index"
            )
        )

    def ask(self, answerer, scores=None, pairs=None):
        found = hits(*(pairs or [(self.chunks[3], 1.0)]))
        with (
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(
                engine, "get_reranker", return_value=LowScoreReranker(scores)
            ),
            mock.patch.object(
                engine.context_builder, "build", wraps=engine.context_builder.build
            ) as build,
        ):
            answer = rag.ask(self.owner, "What does the registry keep records of?")
        return answer, build

    def generative(self, client):
        return answerer_with(client)

    def test_a_model_is_shown_neighbours_and_a_bigger_context(self):
        client = StubChatClient(completion("It keeps records of enrolment [1]."))

        answer, build = self.ask(
            self.generative(client), scores={self.chunks[3].id: 0.9}
        )

        self.assertFalse(answer.abstained, answer.grounding_failures)
        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["budget"], 14000)
        self.assertEqual(kwargs["max_passages"], 12)
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("the text directly before passage [1]", prompt)
        self.assertIn("the text directly after passage [1]", prompt)
        self.assertTrue(
            any("neighbouring passage(s)" in note for note in answer.notes),
            answer.notes,
        )

    @override_settings(RAG_MIN_PASSAGES=1, RAG_ADMIT_RETRIEVAL_TOP=1)
    def test_a_chunk_that_was_retrieved_but_not_admitted_can_still_continue_a_list(
        self,
    ):
        """The chunk after the best one scored under the floor and was left
        out. It is not in the context, so it is exactly the neighbour that
        completes the list; treating "retrieved" as "already shown" dropped it."""
        client = StubChatClient(completion("It keeps records of enrolment [1]."))

        answer, _ = self.ask(
            self.generative(client),
            scores={self.chunks[3].id: 0.9},
            pairs=[(self.chunks[3], 1.0), (self.chunks[4], 0.5)],
        )

        self.assertFalse(answer.abstained, answer.grounding_failures)
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("the text directly before passage [1]", prompt)
        self.assertIn("the text directly after passage [1]", prompt)

    @override_settings(RAG_MODEL_JUDGES_RELEVANCE=False)
    def test_the_bigger_context_does_not_depend_on_the_relevance_switch(self):
        client = StubChatClient(completion("It keeps records of enrolment [1]."))

        answer, build = self.ask(
            self.generative(client), scores={self.chunks[3].id: 0.9}
        )

        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["budget"], 14000)
        self.assertEqual(kwargs["max_passages"], 12)
        prompt = client.calls[0]["messages"][1]["content"]
        self.assertIn("the text directly after passage [1]", prompt)

    def test_a_quote_is_given_neither(self):
        answer, build = self.ask(ExtractiveAnswerer(), scores={self.chunks[3].id: 0.9})

        kwargs = build.call_args.kwargs
        self.assertEqual(kwargs["budget"], 8000)
        self.assertEqual(kwargs["max_passages"], 8)
        self.assertEqual(answer.passages_used, 1)

    def test_a_quote_after_a_provider_failure_never_quotes_a_neighbour(self):
        client = StubChatClient(raises=TimeoutError("timed out"))

        answer, _ = self.ask(self.generative(client), scores={self.chunks[3].id: 0.9})

        self.assertTrue(answer.answerer_fell_back)
        for citation in answer.citations:
            self.assertEqual(citation["chunk_id"], self.chunks[3].id)


# --- A rejected answer gets a chance to be made traceable -------------------

TUITION = "Tuition is 85000 per year for the programme."
HOSTEL = "The hostel fee is 42000 per year for residents."


class RepairCitationsTests(SimpleTestCase):
    def report(self, answer, *passages):
        items = [
            RerankedChunk(chunk=chunk(n, text), score=0.9, signals={})
            for n, text in enumerate(passages, start=1)
        ]
        context = ctx.build(items, semantic_scores=True)
        return context, grounding.validate(answer, context)

    def test_a_figure_in_one_uncited_passage_gets_that_passages_marker(self):
        context, report = self.report(
            "Tuition is 85000 [1] and the hostel fee is 42000.", TUITION, HOSTEL
        )
        self.assertFalse(report.ok)

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [("42000", 2)])
        self.assertEqual(
            repaired, "Tuition is 85000 [1] and the hostel fee is 42000 [2]."
        )
        self.assertTrue(grounding.validate(repaired, context).ok)

    def test_a_figure_in_no_passage_is_not_excused(self):
        context, report = self.report(
            "Tuition is 85000 [1] and the hostel fee is 99999.", TUITION, HOSTEL
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])
        self.assertFalse(grounding.validate(repaired, context).ok)

    def test_a_figure_in_two_passages_is_ambiguous_and_left_alone(self):
        context, report = self.report(
            "Tuition is 85000 [1] and the hostel fee is 42000.",
            TUITION,
            HOSTEL,
            "Hostel fee: 42000 and mess charges 18000.",
        )

        _, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])

    def test_a_small_figure_is_never_attributed(self):
        """ "5" is in most passages; finding it in one means nothing."""
        context, report = self.report(
            "Tuition is 85000 [1] and the fine is 5.",
            TUITION,
            "The late fine is 5 rupees.",
        )

        _, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])

    def test_an_answer_that_cites_nothing_valid_is_not_repaired(self):
        context, report = self.report("The hostel fee is 42000.", TUITION, HOSTEL)
        self.assertFalse(report.ok)

        _, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])

    def test_a_date_can_be_attributed_too(self):
        context, report = self.report(
            "Tuition is 85000 [1], and fees must be paid by 31 March 2025.",
            TUITION,
            "Fees must be paid by 31.03.2025.",
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [("date:2025-03-31", 2)])
        self.assertTrue(grounding.validate(repaired, context).ok)

    def test_a_figure_a_marker_already_follows_is_never_repaired(self):
        """The model attributed it to [1]. If [1] does not state it, the model
        mixed its passages up - the very failure the check exists to catch."""
        context, report = self.report(
            "The B.Tech hostel fee is Rs 85,000 [1].",
            "B.Tech hostel fee per year is Rs 58,000 payable in July.",
            "M.Tech tuition fee per year is Rs 85,000 payable in July.",
        )
        self.assertFalse(report.ok)

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])
        self.assertFalse(grounding.validate(repaired, context).ok)

    def test_a_year_that_is_only_in_another_passages_title_is_not_support(self):
        items = [
            RerankedChunk(
                chunk=chunk(
                    1, "The library opens at 9 am on weekdays.", title="Library"
                ),
                score=0.9,
                signals={},
            ),
            RerankedChunk(
                chunk=chunk(
                    2,
                    "Parking is permitted behind the main building.",
                    title="Campus Notice 2025",
                ),
                score=0.8,
                signals={},
            ),
        ]
        context = ctx.build(items, semantic_scores=True)
        report = grounding.validate(
            "The library rules were revised in 2025 [1].", context
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])
        self.assertFalse(grounding.validate(repaired, context).ok)

    def test_a_marker_never_lands_inside_a_token(self):
        """It split "B.Tech" at the dot and showed "B [2].Tech"."""
        context, report = self.report(
            "Courses are listed [1]. The fee is Rs 85,000 for B.Tech.",
            "General information about the courses offered.",
            "The fee is Rs 85,000 for B.Tech and Rs 90,000 for M.Tech.",
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [("85000", 2)])
        self.assertEqual(
            repaired, "Courses are listed [1]. The fee is Rs 85,000 for B.Tech [2]."
        )
        self.assertTrue(grounding.validate(repaired, context).ok)

    def test_a_figure_before_the_marker_in_a_dotted_sentence_is_left_alone(self):
        context, report = self.report(
            "The fee is Rs 85,000 for B.Tech and Rs 90,000 for M.Tech [1].",
            "General information about the courses offered.",
            "The fee is Rs 85,000 for B.Tech and Rs 90,000 for M.Tech.",
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])
        self.assertEqual(repaired, report.text)

    def test_a_decimal_is_not_laundered_as_two_integers(self):
        """ "3.5" was split at the dot, "3 [2].5" shown, and the unsupported
        figure accepted as two supported ones."""
        context, report = self.report(
            "The tuition is Rs 85,000 for 3.5 years [1].",
            "Clause 3 allows 5 attempts at the examination.",
            "The tuition is Rs 85,000 for the programme. Fees are due in July.",
        )

        repaired, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])
        self.assertFalse(grounding.validate(repaired, context).ok)

    def test_a_clause_about_something_else_is_not_attributed_by_its_figure(self):
        context, report = self.report(
            "Tuition is 85000 [1], and parking costs 42000.", TUITION, HOSTEL
        )

        _, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])

    def test_a_table_row_is_never_repaired(self):
        context, report = self.report(
            "Courses are listed [1].\n| Programme | Fee |\n| B.Tech | 85000 |",
            "General information about the courses offered.",
            "Tuition fee: 85000 for B.Tech programme.",
        )

        _, repairs = grounding.repair_citations(report, context)

        self.assertEqual(repairs, [])

    def test_the_correction_names_the_figures_it_rejected(self):
        _, report = self.report(
            "Tuition is 85000 [1], payable by 31 March 2026.", TUITION
        )

        correction = grounding.correction_for(report)

        # As a person writes the date, not as the check compares it.
        self.assertIn("31 March 2026", correction)
        self.assertNotIn("2026-03-31", correction)
        self.assertIn("rejected by an automated check", correction)


class SequenceClient(StubChatClient):
    """Answers each call with the next text, repeating the last."""

    def __init__(self, *texts):
        super().__init__(completion=None)
        self.texts = list(texts)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return completion(text)


@override_settings(RAG_ANSWERER_PROVIDER="groq")
class RejectedAnswerTests(TestCase):
    """What happens between "the first draft failed grounding" and "not found"."""

    def ask(self, client, *passages, available=True):
        answerer = answerer_with(client)
        # "No model will be tried" is forced on the check itself: the answer is
        # still written through the real gateway and the stub client.
        capacity = (
            contextlib.nullcontext()
            if available
            else mock.patch.object(judging, "model_will_answer", return_value=False)
        )
        found = hits(
            *[
                (chunk(n, text), 1.0 - n / 10)
                for n, text in enumerate(passages, start=1)
            ]
        )
        with (
            mock.patch.object(
                engine.search_service, "search", return_value=FakeSearchResponse(found)
            ),
            mock.patch.object(engine, "get_answerer", return_value=answerer),
            mock.patch.object(
                engine,
                "get_reranker",
                return_value=LowScoreReranker(
                    dict.fromkeys(range(1, len(passages) + 1), 0.9)
                ),
            ),
            mock.patch.object(engine, "_figure_hints", return_value=[]),
            capacity,
        ):
            return rag.ask(None, "What is the tuition per year for the programme?")

    def test_a_missing_citation_is_added_without_asking_again(self):
        client = SequenceClient("Tuition is 85000 [1] and the hostel fee is 42000.")

        answer = self.ask(client, TUITION, HOSTEL)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("[2]", answer.answer)
        self.assertEqual([c["marker"] for c in answer.citations], [1, 2])
        self.assertTrue(
            any("added the citation" in n for n in answer.notes), answer.notes
        )

    def test_a_wrong_figure_is_asked_again_once_and_a_grounded_answer_is_kept(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].", "Tuition is 85000 per year [1]."
        )

        answer = self.ask(client, TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(answer.answer, "Tuition is 85000 per year [1].")
        self.assertEqual(len(client.calls), 2)
        retry_prompt = client.calls[1]["messages"][1]["content"]
        self.assertIn("rejected by an automated check", retry_prompt)
        self.assertIn("90000", retry_prompt)
        self.assertTrue(any("second answer was grounded" in n for n in answer.notes))
        self.assertIn("retry", answer.timings)

    def test_a_second_wrong_figure_leaves_the_first_failure_standing(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].", "Tuition is 95000 per year [1]."
        )

        answer = self.ask(client, TUITION)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertTrue(
            any("90000" in failure for failure in answer.grounding_failures),
            answer.grounding_failures,
        )
        self.assertTrue(
            any("did not produce a grounded answer" in n for n in answer.notes)
        )

    def test_a_model_that_gives_up_on_the_retry_is_still_ungrounded(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].",
            "I couldn't find enough information in the provided documents.",
        )

        answer = self.ask(client, TUITION)

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def recoveries(self):
        return {
            outcome: metrics.REGISTRY.get_sample_value(
                "adi_rag_grounding_recoveries_total", {"outcome": outcome}
            )
            or 0
            for outcome in ("repaired", "retried_grounded", "retried_failed")
        }

    def test_a_retried_answer_is_charged_for_both_calls(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].", "Tuition is 85000 per year [1]."
        )

        answer = self.ask(client, TUITION)

        self.assertEqual(len(client.calls), 2)
        # The stub reports 120 prompt and 18 completion tokens per call.
        self.assertEqual(answer.llm["prompt_tokens"], 240)
        self.assertEqual(answer.llm["completion_tokens"], 36)

    def test_a_retry_that_fails_is_still_charged_for_both_calls(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].", "Tuition is 95000 per year [1]."
        )

        answer = self.ask(client, TUITION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.llm["prompt_tokens"], 240)
        self.assertEqual(answer.llm["completion_tokens"], 36)

    def test_each_recovery_is_counted_by_how_it_ended(self):
        before = self.recoveries()

        self.ask(
            SequenceClient("Tuition is 85000 [1] and the hostel fee is 42000."),
            TUITION,
            HOSTEL,
        )
        self.ask(
            SequenceClient(
                "Tuition is 90000 per year [1].", "Tuition is 85000 per year [1]."
            ),
            TUITION,
        )
        self.ask(
            SequenceClient(
                "Tuition is 90000 per year [1].", "Tuition is 95000 per year [1]."
            ),
            TUITION,
        )
        after = self.recoveries()

        self.assertEqual(
            {key: after[key] - before[key] for key in after},
            {"repaired": 1, "retried_grounded": 1, "retried_failed": 1},
        )

    def test_an_answer_that_passes_first_time_counts_as_no_recovery(self):
        before = self.recoveries()

        self.ask(SequenceClient("Tuition is 85000 per year [1]."), TUITION)

        self.assertEqual(self.recoveries(), before)

    @override_settings(RAG_MODEL_JUDGES_RELEVANCE=False)
    def test_the_retry_does_not_depend_on_the_relevance_switch(self):
        client = SequenceClient(
            "Tuition is 90000 per year [1].", "Tuition is 85000 per year [1]."
        )

        answer = self.ask(client, TUITION)

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(len(client.calls), 2)

    def test_a_failure_inside_the_check_withholds_the_answer(self):
        """The check parses whatever the documents and the model contain. If it
        raises, the answer is withheld and the failure logged - never a 500 and
        never a pass."""
        client = SequenceClient("Tuition is 85000 per year [1].")

        with (
            mock.patch.object(grounding, "validate", side_effect=RuntimeError("boom")),
            self.assertLogs("adi.rag", level="ERROR"),
        ):
            answer = self.ask(client, TUITION)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertTrue(
            any("check itself failed" in f for f in answer.grounding_failures),
            answer.grounding_failures,
        )
        self.assertNotIn("85000", answer.answer)

    def test_a_failure_inside_the_repair_leaves_the_answer_as_it_was(self):
        client = SequenceClient("Tuition is 85000 [1] and the hostel fee is 42000.")

        with (
            mock.patch.object(
                grounding, "repair_citations", side_effect=RuntimeError("boom")
            ),
            self.assertLogs("adi.rag", level="ERROR"),
        ):
            answer = self.ask(client, TUITION, HOSTEL)

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    @override_settings(RAG_GROUNDING_RETRY=False)
    def test_the_retry_can_be_switched_off(self):
        client = SequenceClient("Tuition is 90000 per year [1].")

        answer = self.ask(client, TUITION)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_no_retry_when_no_model_has_capacity_left(self):
        client = SequenceClient("Tuition is 90000 per year [1].")

        answer = self.ask(client, TUITION, available=False)

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(answer.abstained)

    def test_no_retry_for_an_answer_that_states_no_wrong_figure(self):
        """A missing citation is not a figure the passages lack."""
        client = SequenceClient("Tuition is paid every year.")

        answer = self.ask(client, TUITION)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)

    def test_a_fabricated_source_is_not_retried(self):
        client = SequenceClient("Tuition is 85000 per year [7].")

        answer = self.ask(client, TUITION)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)


class ModelWillAnswerTests(SimpleTestCase):
    """The one question everything that is for a model's benefit follows from."""

    def test_a_quote_is_not_a_model(self):
        self.assertFalse(judging.model_will_answer(ExtractiveAnswerer()))

    def test_a_double_whose_attributes_are_all_truthy_is_not_a_model(self):
        """`generative is True`, not truthiness."""
        self.assertFalse(judging.model_will_answer(mock.Mock()))
        self.assertFalse(judging.model_will_answer(mock.Mock(generative="yes")))

    def test_a_model_with_capacity_will_answer(self):
        answerer = mock.Mock(generative=True, gateway=FakeGateway(True))

        self.assertTrue(judging.model_will_answer(answerer))

    def test_a_model_with_no_capacity_left_will_not(self):
        answerer = mock.Mock(generative=True, gateway=FakeGateway(False))

        self.assertFalse(judging.model_will_answer(answerer))

    def test_an_answerer_with_no_budget_to_consult_will(self):
        self.assertTrue(
            judging.model_will_answer(mock.Mock(spec=["generative"], generative=True))
        )

    def test_a_budget_that_cannot_be_read_does_not_decide_the_answer(self):
        class Broken(FakeGateway):
            def plan(self, purpose, **kwargs):
                raise RuntimeError("cache is down")

        answerer = mock.Mock(generative=True, gateway=Broken(True))

        self.assertTrue(judging.model_will_answer(answerer))

    @override_settings(RAG_MODEL_JUDGES_RELEVANCE=False)
    def test_the_relevance_switch_does_not_stop_a_model_answering(self):
        answerer = mock.Mock(generative=True, gateway=FakeGateway(True))

        self.assertTrue(judging.model_will_answer(answerer))
        self.assertFalse(judging.model_can_judge(answerer))

    @override_settings(RAG_MODEL_JUDGES_RELEVANCE=True)
    def test_with_the_switch_on_both_agree(self):
        answerer = mock.Mock(generative=True, gateway=FakeGateway(True))

        self.assertTrue(judging.model_can_judge(answerer))

    @override_settings(RAG_PLAN_WHEN_GENERATIVE=False, RAG_MODEL_JUDGES_RELEVANCE=False)
    def test_a_model_still_gets_the_whole_question_with_the_switch_off(self):
        answerer = mock.Mock(generative=True, gateway=FakeGateway(True))
        calls = []

        def ask(user, question, **kwargs):
            calls.append(question)
            return rag.RagAnswer(
                question=question, answer=ABSTENTION_TEXT, abstained=True
            )

        with (
            mock.patch.object(runner, "ask", side_effect=ask),
            mock.patch.object(runner, "get_answerer", return_value=answerer),
        ):
            agents.answer(None, THREE_PART)

        self.assertEqual(calls, [THREE_PART])


class CapacityStatusTests(GatewayTestCase):
    """A free tier's tokens-per-minute window answers 413, and 413 comes back
    by itself like a 429."""

    def test_413_is_capacity_like_429(self):
        for status in (413, 429, 503):
            with self.subTest(status=status):
                self.assertTrue(gateway_module.is_capacity(StatusError(status)))
        self.assertFalse(gateway_module.is_capacity(StatusError(401)))
        self.assertFalse(gateway_module.is_capacity(StatusError(500)))

    def test_413_behind_an_unavailable_is_capacity_too(self):
        unavailable = gateway_module.Unavailable(gateway_module.FAILED)
        unavailable.__cause__ = StatusError(413)

        self.assertTrue(gateway_module.is_capacity(unavailable))

    def test_a_413_rests_the_link_instead_of_breaking_it(self):
        chain = build("groq")
        wire(chain, "groq", ModelClient({"openai/gpt-oss-120b": 413}))

        with self.assertRaises(gateway_module.Unavailable) as caught:
            chain.complete(
                [{"role": "user", "content": "q"}],
                purpose=gateway_module.CHAT,
                max_tokens=8,
            )

        self.assertTrue(gateway_module.is_capacity(caught.exception))
        self.assertFalse(chain.available(gateway_module.CHAT))


# --- Vector search stays exact ----------------------------------------------


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER="documents.processing.embedding.hashing.HashingEmbeddingProvider",
)
class VectorSearchStaysExactTests(TestCase):
    """An HNSW scan returns at most `hnsw.ef_search` rows BEFORE the permission
    filter is applied. Where someone may read a small share of the index (one
    department of many, under open sign-up), almost none of the nearest rows
    would be theirs, and the semantic half of search would come back nearly
    empty - so chat would be answering from lexical results alone. Today that
    cannot happen only because `_vector` orders by (distance, id): no HNSW
    index can serve an ORDER BY with a key it does not hold, on Postgres 16.

    That rests on a second column name and a Postgres version. A plan cannot
    pin it - on a table this small Postgres never walks the index whatever the
    query, so a plan test passes with or without the tie-break and proves
    nothing - so the ORDER BY itself is what is pinned."""

    def test_the_search_query_orders_by_distance_and_then_id(self):
        from documents.models import DocumentChunkEmbedding

        owner = make_user("plan_owner", department="Registry")
        index_handbook(owner)
        queryset, _ = search_service.authorized_chunks(owner)
        row = DocumentChunkEmbedding.objects.first()

        built = search_service._vector_queryset(
            queryset, [0.1] * 384, row.model_name, row.model_version, 5
        )

        self.assertEqual(built.query.order_by, ("distance", "id"))
        self.assertTrue(0 < len(list(built)) <= 5)


class RestingLinkTests(GatewayTestCase):
    """Chat tries a link that is resting after a rate limit rather than give
    up, and on a free tier most links are resting most of the time. Asking for
    a READY one switched the whole model-judged path off exactly when the site
    was busiest."""

    def rested(self):
        chain = build("groq")
        wire(chain, "groq", ModelClient({"openai/gpt-oss-120b": 429}))
        with self.assertRaises(gateway_module.Unavailable):
            chain.complete(
                [{"role": "user", "content": "q"}],
                purpose=gateway_module.CHAT,
                max_tokens=8,
            )
        return chain

    def test_a_resting_link_still_counts_for_chat(self):
        chain = self.rested()
        answerer = mock.Mock(generative=True, gateway=chain)

        self.assertFalse(chain.available(gateway_module.CHAT))
        self.assertTrue(judging.model_can_judge(answerer))

    def test_but_not_for_work_that_waits_its_turn(self):
        chain = self.rested()
        answerer = mock.Mock(generative=True, gateway=chain)

        with gateway_module.purpose(gateway_module.BRIEF):
            self.assertFalse(judging.model_can_judge(answerer))

    def test_a_chain_with_no_key_is_never_tried(self):
        chain = build("groq")
        answerer = mock.Mock(generative=True, gateway=chain)

        self.assertFalse(judging.model_can_judge(answerer))
