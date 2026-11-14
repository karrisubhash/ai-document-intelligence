"""Answer-quality fixes: false abstentions, and what reaches the model.

Each class pins one way a correct answer used to be lost, and the guard
that keeps the fix from turning into an invented one.
"""

from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from documents import agents, rag
from documents.agents import runner
from documents.models import Document, ProcessingJob
from documents.processing.pipeline import run_pipeline
from documents.rag import context as ctx
from documents.rag import engine, facts, followup, grounding, judging
from documents.rag.gateway import Gateway
from documents.rag.llm import ABSTENTION_TEXT, ExtractiveAnswerer, declines_to_answer
from documents.rag.reranking import RerankedChunk
from documents.tests import MEDIA_ROOT, make_user
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
    """A gateway that only answers `available`."""

    def __init__(self, available):
        self._available = available

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
        answerer = answerer_with(StubChatClient(completion(reply)))
        # Capacity is a budget lookup; the answer itself still runs through
        # the real gateway and the stub client.
        answerer.gateway.available = lambda purpose: True
        return answerer

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

    def test_a_date_that_does_not_exist_is_left_alone(self):
        _, dates = facts.extract_dates("On 31 February 2025 and 31.02.2025.")
        self.assertEqual(dates, set())

    def test_a_year_range_is_one_range(self):
        self.assertEqual(facts.canonical_range("2024-25"), "2024-2025")
        self.assertEqual(facts.canonical_range("1999-00"), "1999-2000")
        # Not a range of consecutive years: left as it is.
        self.assertEqual(facts.canonical_range("2024-05"), "2024-05")
        self.assertEqual(facts.canonical_range("2024-2025"), "2024-2025")
        self.assertEqual(facts.prepare("2024\N{EN DASH}25"), "2024-25")
        self.assertEqual(facts.range_years("2024-2025"), {"2024", "2025"})

    def test_number_words_stand_for_numerals(self):
        self.assertEqual(
            facts.number_words("within seven days, twenty-five rupees, the third"),
            {"7", "25", "3"},
        )
        self.assertEqual(facts.number_words("a statement of the situation"), set())

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

    def test_a_marker_one_past_the_last_passage_beside_a_real_one_is_removed(self):
        report = self.validate("Attendance must be 75 percent [1] [3].")

        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.text, "Attendance must be 75 percent [1].")
        self.assertEqual(report.cited_markers, (1,))

    def test_a_marker_zero_beside_a_real_one_is_removed(self):
        report = self.validate("Attendance must be 75 percent [0][1].")

        self.assertTrue(report.ok, report.failures)
        self.assertNotIn("[0]", report.text)

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

    def test_the_stray_marker_is_not_shown(self):
        answer = self.ask("You need 75 percent attendance [1] [2].")

        self.assertFalse(answer.abstained, answer.grounding_failures)
        self.assertEqual(answer.answer, "You need 75 percent attendance [1].")
        self.assertEqual([c["marker"] for c in answer.citations], [1])

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

    def ask(self, answerer, scores=None):
        found = hits((self.chunks[3], 1.0))
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
        answerer = answerer_with(client)
        answerer.gateway.available = lambda purpose: True
        return answerer

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
