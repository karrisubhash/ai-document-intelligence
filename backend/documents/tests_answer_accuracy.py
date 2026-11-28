"""Answer accuracy: the failures found in the live chat log, 2026-11-25.

Each test pins one of them. The questions are the ones people asked on the
public site (paraphrased where they named a person), and the passages are the
shapes the documents had: a lecture's table with glued dates, a title with a
date and a time in it, a definition one word long, a library question.
"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import UserProfile
from documents.agents.runner import PlannedAnswer
from documents.models import (
    Conversation,
    Document,
    DocumentChunk,
    DocumentContent,
    DocumentInsight,
    Message,
    ProcessingJob,
)
from documents.rag import context as ctx
from documents.rag import engine, facts, followup, labels, library
from documents.rag.llm import ExtractiveAnswerer
from documents.rag.reranking import RerankedChunk
from documents.search.service import SearchHit, SearchResponse
from documents.tests import MEDIA_ROOT, make_user
from documents.tests_fix_grounding import Base, build
from documents.tests_rag import chunk

CHAT = "/api/v1/chat/"


def turn(role, content):
    return SimpleNamespace(role=role, content=content, abstained=False)


# --- Follow-ups: a one-word question may be a new topic ---------------------


class OneWordQuestionTests(SimpleTestCase):
    HISTORY = [
        turn("USER", "What is error budget?"),
        turn("ASSISTANT", "Error budget is ..."),
        turn("USER", "Tell about GAGAN"),
        turn("ASSISTANT", "GAGAN is ..."),
    ]

    def test_a_bare_term_after_another_topic_is_searched_both_ways(self):
        # Live: "EUROCONTROL" was searched only as "EUROCONTROL gagan error
        # budget", and the passage that defines it was not in the answer.
        resolved = followup.contextualize("EUROCONTROL", self.HISTORY)
        self.assertTrue(resolved.rewritten)
        self.assertTrue(resolved.ambiguous)
        self.assertEqual(resolved.search_also, "EUROCONTROL")
        self.assertIn("gagan", resolved.question)
        self.assertTrue(resolved.as_metadata["searched_both"])

    def test_a_question_that_refers_back_is_not_a_guess(self):
        resolved = followup.contextualize("What about MECH?", self.HISTORY)
        self.assertTrue(resolved.rewritten)
        self.assertFalse(resolved.ambiguous)
        self.assertEqual(resolved.search_also, "")

    def test_no_content_word_means_only_a_follow_up(self):
        resolved = followup.contextualize("tell me more", self.HISTORY)
        self.assertEqual(resolved.search_also, "")

    def test_an_independent_question_is_untouched(self):
        question = "What is the accuracy of the GAGAN signal?"
        resolved = followup.contextualize(question, self.HISTORY)
        self.assertFalse(resolved.rewritten)
        self.assertEqual(resolved.search_also, "")
        self.assertFalse(resolved.as_metadata["searched_both"])

    def test_greetings_people_here_use_are_small_talk(self):
        # Live: "Hai" was read as a follow-up, searched as "Hai eurocontrol"
        # and answered "I couldn't find enough information".
        for greeting in (
            "Hai",
            "hai",
            "Haii",
            "hlo",
            "Hlo there",
            "Namaste",
            "namaskaram",
        ):
            with self.subTest(greeting=greeting):
                self.assertTrue(followup.is_small_talk(greeting))
        for question in ("Hai eurocontrol", "hostel", "Hall ticket"):
            with self.subTest(question=question):
                self.assertFalse(followup.is_small_talk(question))

    def test_a_greeting_is_not_rewritten(self):
        resolved = followup.contextualize("Hai", self.HISTORY)
        self.assertFalse(resolved.rewritten)


class PooledRetrievalTests(SimpleTestCase):
    def hit(self, identifier, score=1.0):
        return SearchHit(chunk=SimpleNamespace(id=identifier), score=score)

    def test_two_searches_interleave_and_each_chunk_appears_once(self):
        first = SearchResponse(
            hits=[self.hit(1), self.hit(2), self.hit(3)], notes=["a"]
        )
        second = SearchResponse(
            hits=[self.hit(9), self.hit(2), self.hit(8), self.hit(7)],
            notes=["a", "b"],
            semantic=True,
        )
        pooled = engine.pooled_retrieval(first, second)
        self.assertEqual([hit.chunk.id for hit in pooled.hits], [1, 9, 2, 3, 8, 7])
        self.assertTrue(pooled.semantic)
        self.assertEqual(pooled.notes, ["a", "b"])

    def test_each_passage_keeps_its_better_score(self):
        def item(identifier, score):
            return RerankedChunk(chunk=SimpleNamespace(id=identifier), score=score)

        widened = [item(1, 0.9), item(2, 0.2), item(3, 0.1)]
        bare = [item(3, 0.95), item(2, 0.1), item(1, 0.05)]
        best = engine.better_scores(widened, bare)
        self.assertEqual(
            [(i.chunk.id, i.score) for i in best], [(3, 0.95), (1, 0.9), (2, 0.2)]
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ChatPassesBothReadingsTests(TestCase):
    def test_the_chat_view_hands_retrieval_the_bare_word(self):
        user = make_user("reader", role=UserProfile.Role.STUDENT, department="")
        conversation = Conversation.objects.create(owner=user)
        Message.objects.create(
            conversation=conversation, role="USER", content="Tell about GAGAN"
        )
        Message.objects.create(
            conversation=conversation, role="ASSISTANT", content="GAGAN is ..."
        )
        client = APIClient()
        client.force_authenticate(user)
        canned = PlannedAnswer(question="EUROCONTROL", answer="x", abstained=True)
        with mock.patch(
            "documents.chat.views.agents.answer", return_value=canned
        ) as answer:
            response = client.post(
                CHAT,
                {"question": "EUROCONTROL", "conversation_id": conversation.id},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        kwargs = answer.call_args.kwargs
        self.assertEqual(kwargs["search_also"], "EUROCONTROL")
        self.assertEqual(kwargs["focus"], "EUROCONTROL")
        self.assertIn("gagan", answer.call_args.args[1])

    def test_planned_parts_do_not_carry_the_second_reading(self):
        from documents.agents import runner

        seen = []

        def ask(user, question, **kwargs):
            seen.append(kwargs.get("search_also"))
            return mock.MagicMock(
                abstained=True,
                answer="",
                citations=[],
                notes=[],
                timings={},
                model_name="",
            )

        with (
            mock.patch.object(runner, "ask", side_effect=ask),
            mock.patch.object(
                runner.planner, "plan", return_value=["part one", "part two"]
            ),
            mock.patch.object(runner.judging, "model_will_answer", return_value=False),
        ):
            try:
                runner.answer(object(), "part one and part two", search_also="word")
            except Exception:  # noqa: BLE001 - only the calls matter here
                pass
        self.assertTrue(seen)
        self.assertTrue(all(value == "" for value in seen), seen)


# --- Grounding: correct answers that were refused ---------------------------


class GluedDateTests(SimpleTestCase):
    def test_glued_dates_are_spaced_before_figures_are_read(self):
        self.assertIn("22 Feb. 1978", facts.prepare("PRN 4 22Feb.1978 Kosmos"))
        self.assertIn("28 Dec, 2005", facts.prepare("GIOVE-A 28Dec,2005"))
        self.assertIn("01 Oct 2026", facts.prepare("Lecture IV GNSS 01Oct2026 1600hrs"))

    def test_a_code_is_not_a_date(self):
        for text in ("REF12Jan2024", "123Jan2024", "12Janx2024"):
            with self.subTest(text=text):
                self.assertEqual(facts.prepare(text), facts.clean(text))


class GroundingFixTests(Base):
    TABLE = (
        "Parameter NAVSTAR GPS GLONASS Galileo Beidou1/2 "
        "1st Satellite PRN 4 Kosmos1413 GIOVE-A Beidou-1A "
        "22Feb.1978 12Oct.1982 28Dec,2005 30Oct.2000"
    )

    def test_a_date_the_table_glues_supports_the_date_written_out(self):
        # Live: "launched on 22 February 1978" was refused for "1978" and "22".
        self.accepted(
            "The first NAVSTAR GPS satellite was launched on 22 February 1978 [1].",
            self.TABLE,
        )
        self.accepted("The first GPS satellite went up in 1978 [1].", self.TABLE)

    def test_a_title_s_date_and_time_may_be_repeated(self):
        # Live: a summary naming the lecture was refused for "01Oct2026" and
        # "1600hrs", which are the document's own title.
        self.accepted(
            "The lecture 'Lecture IV GNSS 01Oct2026 1600hrs' describes SBAS [1].",
            "SBAS augments GNSS signals.",
            titles=("Lecture IV GNSS 01Oct2026 1600hrs",),
        )

    def test_a_title_s_bare_number_still_supports_nothing(self):
        self.rejected(
            "Fees are paid in 3 instalments [1].",
            "Fees are paid in instalments.",
            titles=("Fee Rules Revision 3",),
            figure="3",
        )

    def test_a_figure_with_a_word_hyphenated_on_is_that_figure(self):
        # Live: "APV1-certified" against "APV1 Certified GAGAN signals".
        self.accepted(
            "GAGAN signals are APV1-certified [1].",
            "APV1 Certified GAGAN signals are being broadcast with effect from May 19, 2015.",
        )
        self.rejected(
            "GAGAN signals are APV2-certified [1].",
            "APV1 Certified GAGAN signals are being broadcast.",
            figure="apv2-certified",
        )

    def page_context(self, page):
        item = chunk(1, "A subcircuit is drawn as a block with its pins.")
        item = replace(item, page_number=page)
        return ctx.build(
            [RerankedChunk(chunk=item, score=0.9, signals={})],
            semantic_scores=True,
            budget=10**9,
            max_passages=50,
        )

    def test_a_reference_to_a_page_shown_is_not_a_claim(self):
        from documents.rag import grounding

        report = grounding.validate(
            "A subcircuit is drawn as a block with its pins (page 22) [1].",
            self.page_context(22),
        )
        self.assertTrue(report.ok, (report.failures, report.unsupported_facts))

    def test_a_page_no_passage_came_from_is_still_checked(self):
        from documents.rag import grounding

        report = grounding.validate(
            "A subcircuit is drawn as a block with its pins (page 99) [1].",
            self.page_context(22),
        )
        self.assertIn("99", report.unsupported_facts)


class GluedIsbnTests(Base):
    BOOKS = (
        "Reference books: Higher Engineering Mathematics, K. A. Stroud, "
        "ISBN-13: 978-81-203-4567-8, Prentice Hall."
    )

    def test_an_isbn_with_its_length_label_glued_on_is_that_isbn(self):
        # Live (2026-11-26): a model wrote "ISBN-13978-81-..." for "ISBN-13:
        # 978-81-...", the answer was refused twice for a figure no passage
        # had, and the reader got a bare quote.
        for written in (
            "ISBN 13978-81-203-4567-8",
            "ISBN-13978-81-203-4567-8",
            "ISBN13978-81-203-4567-8",
            "ISBN-13:978-81-203-4567-8",
        ):
            with self.subTest(written=written):
                self.accepted(
                    f"The reference book is Stroud's Higher Engineering Mathematics, "
                    f"{written} [1].",
                    self.BOOKS,
                )

    def test_a_different_isbn_is_still_refused(self):
        self.rejected(
            "The reference book is Stroud's, ISBN 13978-81-203-9999-9 [1].",
            self.BOOKS,
            figure="978-81-203-9999-9",
        )

    def test_a_number_that_is_not_an_isbn_stays_whole(self):
        self.rejected(
            "The hall seats 13978 students [1].",
            "The hall seats 978 students.",
            figure="13978",
        )


# --- Quoting a table of labelled rows ---------------------------------------

FEES = "\n".join(
    [
        "Table 1, row 1: Programme: B.Tech CSE; Tuition: 85000; Exam Fee: 2500; Hostel: 60000",
        "Table 1, row 2: Programme: B.Tech ECE; Tuition: 82000; Exam Fee: 2500; Hostel: 60000",
        "Table 1, row 3: Programme: B.Tech MECH; Tuition: 78000; Exam Fee: 2500; Hostel: 58000",
        "Table 1, row 4: Programme: M.Tech CSE; Tuition: 95000; Exam Fee: 3000; Hostel: 60000",
    ]
)


class LabelledTableQuoteTests(SimpleTestCase):
    def quote(self, question):
        context = build(FEES, titles=("Fee Structure",))
        return ExtractiveAnswerer().answer(
            question=question, context=context, prompt=""
        )

    def test_a_question_that_names_only_a_column_gets_the_whole_table(self):
        # v3 gate: three of four rows were quoted for "the lowest tuition", and
        # not the MECH row that answers it.
        response = self.quote("Which programme has the lowest tuition?")
        self.assertFalse(response.abstained)
        for figure in ("85000", "82000", "78000", "95000"):
            self.assertIn(figure, response.text)

    def test_a_question_that_names_a_row_still_gets_that_row(self):
        response = self.quote("What is the tuition for B.Tech MECH?")
        self.assertIn("78000", response.text)
        self.assertNotIn("95000", response.text)


# --- Questions about the library itself -------------------------------------


class ClassifyTests(SimpleTestCase):
    def test_the_live_library_questions(self):
        cases = {
            "What are the documents uploaded": "list",
            "Uploaded document related to?": "summary",
            "What Tells the uploaded document": "summary",
            "Recently uploaded document summary": "summary",
            "list my files": "list",
            "how many documents do I have": "list",
        }
        for question, kind in cases.items():
            with self.subTest(question=question):
                self.assertEqual(library.classify(question), (kind, []))

    def test_questions_about_content_or_using_the_platform_are_not(self):
        for question in (
            "What documents are required for admission?",
            "What is the last date to upload documents?",
            "How do I upload a document?",
            "Can I upload a PDF?",
            "what was the page limit for pdf's in this project",
            "What is the attendance policy?",
        ):
            with self.subTest(question=question):
                self.assertIsNone(library.classify(question))

    def test_a_summary_with_other_words_keeps_them_for_the_title(self):
        self.assertEqual(
            library.classify("Summarise the GNSS lecture document"),
            ("summary", ["gnss", "lecture"]),
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class LibraryAnswerTests(TestCase):
    def setUp(self):
        self.owner = make_user("uploader", role=UserProfile.Role.STUDENT, department="")
        self.other = make_user("stranger", role=UserProfile.Role.STUDENT, department="")
        now = timezone.now()
        self.gnss = self.make(
            self.owner,
            "Lecture IV GNSS 01Oct2026 1600hrs",
            now - timedelta(days=2),
            summary=["GAGAN is the first SBAS system to serve the equatorial region."],
            pages=49,
        )
        self.cv = self.make(
            self.owner,
            "Student CV",
            now - timedelta(days=1),
            summary=[
                "Mechanical engineering student with an interest in backend work."
            ],
            pages=1,
        )
        self.failed = self.make(
            self.owner,
            "Garuda Puranam",
            now,
            status=Document.Status.FAILED,
            error="PDF has 792 pages, over the 500 page limit.",
        )
        self.hidden = self.make(
            self.other, "Someone else's notes", now, summary=["Private."]
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def make(
        self, owner, title, created, *, summary=(), pages=1, status=None, error=""
    ):
        document = Document.objects.create(
            title=title,
            owner=owner,
            department="",
            status=status or Document.Status.COMPLETED,
        )
        Document.objects.filter(pk=document.pk).update(created_at=created)
        version = document.versions.create(
            version_number=1,
            file=ContentFile(b"x", name="x.pdf"),
            file_name="x.pdf",
            file_size=1,
            uploaded_by=owner,
        )
        content = DocumentContent.objects.create(
            document_version=version, processing_version="test", page_count=pages
        )
        version.current_content = content
        version.save(update_fields=["current_content"])
        document.current_version = version
        document.save(update_fields=["current_version"])
        if error:
            ProcessingJob.objects.create(
                document_version=version,
                status=ProcessingJob.Status.FAILED,
                error_message=error,
            )
        text = " ".join(summary) or title
        DocumentChunk.objects.create(
            document_version=version,
            document_content=content,
            chunk_index=0,
            page_number=1,
            character_start=0,
            character_end=len(text),
            text=text,
            character_count=len(text),
            chunking_strategy="test",
            chunking_version="test",
        )
        if summary:
            DocumentInsight.objects.create(
                document_version=version,
                document_content=content,
                status=DocumentInsight.Status.READY,
                generator_name="test",
                generator_version="1",
                summary=[
                    {
                        "text": sentence,
                        "page_number": 1,
                        "start": 0,
                        "end": len(sentence),
                        "score": 1.0,
                    }
                    for sentence in summary
                ],
            )
        document.refresh_from_db()
        return document

    def ask(self, question, **extra):
        canned = PlannedAnswer(
            question=question, answer="from retrieval", abstained=True
        )
        with mock.patch(
            "documents.chat.views.agents.answer", return_value=canned
        ) as rag:
            response = self.client.post(
                CHAT, {"question": question, **extra}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json(), rag

    def test_what_documents_are_uploaded_lists_the_askers_documents(self):
        body, rag = self.ask("What are the documents uploaded")
        rag.assert_not_called()
        self.assertEqual(body["source"], "documents")
        self.assertFalse(body["abstained"])
        self.assertEqual(body["model_display"], labels.LIBRARY)
        answer = body["answer"]
        self.assertIn("Student CV", answer)
        self.assertIn("Lecture IV GNSS 01Oct2026 1600hrs", answer)
        # Newest first, and only what the asker may read.
        self.assertLess(answer.index("Student CV"), answer.index("Lecture IV GNSS"))
        self.assertNotIn("Someone else", answer)
        # The document that failed is named, with why.
        self.assertIn("Garuda Puranam", answer)
        self.assertIn("792 pages", answer)
        cited = {citation["document_id"] for citation in body["citations"]}
        self.assertEqual(cited, {self.cv.id, self.gnss.id})
        self.assertEqual([c["marker"] for c in body["citations"]], [1, 2])

    def test_the_recently_uploaded_document_summary_is_its_brief(self):
        body, rag = self.ask("Recently uploaded document summary")
        rag.assert_not_called()
        self.assertIn(
            "most recently uploaded document is **Student CV**", body["answer"]
        )
        self.assertIn("backend work", body["answer"])
        self.assertEqual(body["citations"][0]["document_id"], self.cv.id)

    def test_a_document_named_by_its_title_is_summarised(self):
        body, rag = self.ask("Summarise the GNSS lecture document")
        rag.assert_not_called()
        self.assertIn("GAGAN is the first SBAS system", body["answer"])

    def test_a_chat_about_one_document_summarises_that_one(self):
        body, rag = self.ask(
            "What does this document contain?", document_id=self.gnss.id
        )
        rag.assert_not_called()
        self.assertIn("GAGAN", body["answer"])
        self.assertTrue(
            all(c["document_id"] == self.gnss.id for c in body["citations"])
        )

    def test_a_question_about_content_goes_to_retrieval(self):
        body, rag = self.ask("What does the document say about EUROCONTROL?")
        rag.assert_called_once()

    def test_a_document_someone_else_owns_cannot_be_summarised_by_name(self):
        body, rag = self.ask("Summarise someone else's notes document")
        rag.assert_called_once()

    def test_no_documents(self):
        client = APIClient()
        client.force_authenticate(
            make_user("empty", role=UserProfile.Role.STUDENT, department="")
        )
        response = client.post(CHAT, {"question": "list my documents"}, format="json")
        self.assertIn("no documents", response.json()["answer"])
