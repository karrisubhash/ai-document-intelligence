"""Answer-quality fixes: false abstentions, and what reaches the model.

Each class pins one way a correct answer used to be lost, and the guard
that keeps the fix from turning into an invented one.
"""

from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from documents import rag
from documents.models import Document, ProcessingJob
from documents.processing.pipeline import run_pipeline
from documents.rag import context as ctx
from documents.rag import engine, followup, judging
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
