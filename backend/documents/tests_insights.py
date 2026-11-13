"""The document brief: segmentation, entities, summary, enrichment.

The engine's rules are tested on plain strings (no database); the
enrichment task is tested through real uploads, because what matters there
is the order of events - publish, then enrich - and what survives a retry.
"""

import datetime
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from documents.models import (
    Document,
    DocumentCentroid,
    DocumentEntity,
    DocumentInsight,
    DocumentThumbnail,
    ExtractionResult,
    ProcessingEvent,
    ProcessingJob,
)
from documents.processing.insights import service as insight_service
from documents.processing.insights.enrichment import enrich_version
from documents.processing.insights.entities import extract_entities
from documents.processing.insights.questions import Candidate, choose
from documents.processing.insights.stats import compute_stats, detect_language
from documents.processing.insights.summary import key_phrases, key_points, summarize
from documents.processing.insights.text import segment
from documents.tests_ingestion import IngestionTestCase, make_pdf_bytes

CIRCULAR = """CIRCULAR TR/2024/117
Institute of Technology - Office of the Registrar
Subject: Campus bus services for the academic year 2024-25

1. Routes and timings
Buses run on six city routes. On weekdays the first bus leaves the city
terminus at 7 am and the last bus leaves the campus at 7.30 pm. On Saturdays
the last bus leaves the campus at 2 pm. There is no service on Sundays.

2. Bus pass
A bus pass costs Rs. 4,500/- per semester and is issued on payment at the
Transport Section. A student must show the bus pass together with the
institute identity card when boarding.

3. Renewal
The last date for renewal of the bus pass is 15th March 2027. Late renewal
attracts a fine of 200 rupees. Queries: transport@iitx.ac.in or Phone
040-2345 6789. Details at www.iitx.ac.in/transport.
"""


def entities_of(text):
    return {(e.entity_type, e.normalized): e for e in extract_entities([(1, text)])}


class SegmentationTests(SimpleTestCase):
    def test_every_segment_re_slices_exactly(self):
        for segment_ in segment([(1, CIRCULAR), (2, "Short page.\nNo end")]):
            source = CIRCULAR if segment_.page_number == 1 else "Short page.\nNo end"
            self.assertEqual(source[segment_.start : segment_.end], segment_.text)

    def test_headings_and_wrapped_sentences(self):
        segments = segment([(1, CIRCULAR)])
        headings = [s.text for s in segments if s.is_heading]
        self.assertIn("1. Routes and timings", headings)
        self.assertIn("2. Bus pass", headings)
        # A sentence wrapped over two lines is one segment.
        self.assertTrue(
            any(
                s.text.startswith("On weekdays the first bus") and "7.30 pm." in s.text
                for s in segments
            )
        )

    def test_abbreviations_and_decimals_do_not_end_sentences(self):
        text = "The fee is Rs. 4.50 per page and No. 7 applies. Next sentence here."
        texts = [s.text for s in segment([(1, text)])]
        self.assertEqual(
            texts,
            ["The fee is Rs. 4.50 per page and No. 7 applies.", "Next sentence here."],
        )

    def test_a_table_row_is_not_a_heading(self):
        text = "Programme\tTuition\nB.Tech CSE\t85000\t2500\n"
        self.assertFalse(any(s.is_heading for s in segment([(1, text)])))


class EntityTests(SimpleTestCase):
    def test_dates_are_read_day_first_and_normalised(self):
        found = entities_of("Issued on 12/03/2024. Exam on 25-04-2024. 2024-05-06.")
        self.assertIn(("DATE", "2024-03-12"), found)
        self.assertIn(("DATE", "2024-04-25"), found)
        self.assertIn(("DATE", "2024-05-06"), found)

    def test_an_impossible_date_is_not_normalised_into_a_guess(self):
        found = extract_entities([(1, "Due 31/02/2024 is not a date.")])
        self.assertFalse(any(e.normalized == "2024-02-31" for e in found))

    def test_a_deadline_carries_what_it_is_for(self):
        found = entities_of(CIRCULAR)
        deadline = found[("DEADLINE", "2027-03-15")]
        self.assertEqual(deadline.text, "15th March 2027")
        self.assertEqual(deadline.value_date, datetime.date(2027, 3, 15))
        self.assertEqual(deadline.label, "Last date for renewal of the bus pass")

    def test_on_or_before_makes_a_deadline(self):
        found = entities_of("Applications must be submitted on or before 30/04/2027.")
        deadline = found[("DEADLINE", "2027-04-30")]
        self.assertEqual(deadline.label, "Applications must be submitted")

    def test_money_in_its_indian_forms(self):
        found = entities_of(
            "Fee Rs. 1,20,000/- or ₹ 500 or 4500 rupees or Rs 2 lakh or USD 300."
        )
        for value in ("INR 120000", "INR 500", "INR 4500", "INR 200000", "USD 300"):
            self.assertIn(("MONEY", value), found)

    def test_percent_duration_contacts(self):
        found = entities_of(
            "75 percent attendance within 15 days. Mail exams@iitx.ac.in or "
            "https://iitx.ac.in/Exams. Mobile 98765 43210."
        )
        self.assertIn(("PERCENT", "75%"), found)
        self.assertIn(("DURATION", "P15D"), found)
        self.assertIn(("EMAIL", "exams@iitx.ac.in"), found)
        self.assertIn(("URL", "https://iitx.ac.in/Exams"), found)
        self.assertIn(("PHONE", "+919876543210"), found)

    def test_numbers_without_a_phone_cue_are_not_phones(self):
        found = extract_entities([(1, "Tuition 85000 65000\nRoll 9876543210")])
        self.assertFalse(any(e.entity_type == "PHONE" for e in found))

    def test_references_codes_and_organisations(self):
        found = entities_of(
            "CIRCULAR TR/2024/117\nACADEMIC REGULATIONS R20\nCourse CS301 and MA 102. "
            "Fee INR 5000 for FY 2024. Office of the Registrar, Osmania University."
        )
        self.assertIn(("REFERENCE", "TR/2024/117"), found)
        self.assertIn(("REFERENCE", "R20"), found)
        self.assertIn(("COURSE_CODE", "CS301"), found)
        self.assertIn(("COURSE_CODE", "MA102"), found)
        self.assertNotIn(("COURSE_CODE", "INR5000"), found)
        self.assertIn(("ORG", "Office of the Registrar"), found)
        self.assertIn(("ORG", "Osmania University"), found)

    def test_duplicates_are_counted_not_repeated(self):
        found = extract_entities([(1, "Pay 75 percent. Again 75 percent."), (2, "75%")])
        percents = [e for e in found if e.entity_type == "PERCENT"]
        self.assertEqual(len(percents), 1)
        self.assertEqual(percents[0].occurrences, 3)
        self.assertEqual((percents[0].page_number, percents[0].start), (1, 4))

    def test_every_entity_re_slices_exactly(self):
        for entity in extract_entities([(1, CIRCULAR)]):
            self.assertEqual(CIRCULAR[entity.start : entity.end], entity.text)


class SummaryTests(SimpleTestCase):
    def test_a_summary_is_three_of_the_documents_own_sentences_in_order(self):
        segments = segment([(1, CIRCULAR)])
        summary, info = summarize(segments)
        self.assertEqual(len(summary), 3)
        positions = [s.position for s, _ in summary]
        self.assertEqual(positions, sorted(positions))
        for sentence, _ in summary:
            self.assertEqual(CIRCULAR[sentence.start : sentence.end], sentence.text)
            self.assertFalse(sentence.is_heading)

    def test_sentences_inside_a_table_are_not_summarised(self):
        text = "The fee table follows below for every programme offered here.\n" * 3
        segments = segment([(1, text)])
        summary, _ = summarize(segments, excluded_regions=[(1, 0, len(text))])
        self.assertEqual(summary, [])

    def test_key_points_say_why_they_were_chosen(self):
        segments = segment([(1, CIRCULAR)])
        entities = extract_entities([(1, CIRCULAR)])
        points = key_points(segments, entities)
        kinds = {kind for _, kind in points}
        self.assertIn("deadline", kinds)
        self.assertIn("obligation", kinds)
        self.assertLessEqual(len(points), 6)

    def test_key_phrases_skip_stopwords_and_numbers(self):
        phrases = [text for text, _ in key_phrases([(1, CIRCULAR)])]
        # The phrase, not its parts: "bus" is folded into "bus pass".
        self.assertIn("bus pass", phrases)
        self.assertNotIn("bus", phrases)
        for phrase in phrases:
            self.assertFalse(any(ch.isdigit() for ch in phrase))
            self.assertNotIn(phrase, {"the", "and", "clause"})


class StatsTests(SimpleTestCase):
    def test_language_by_script(self):
        self.assertEqual(detect_language(CIRCULAR), "en")
        self.assertEqual(detect_language("यह एक परिपत्र है जो सभी छात्रों के लिए है"), "hi")
        self.assertEqual(detect_language("ఇది అన్ని విద్యార్థుల కోసం ఒక సర్క్యులర్"), "te")
        self.assertEqual(
            detect_language(
                "This circular is for all students. यह परिपत्र सभी छात्रों के लिए"
            ),
            "mixed",
        )
        self.assertEqual(detect_language("12 34"), "unknown")

    def test_stats(self):
        stats = compute_stats([(1, CIRCULAR, "PDF_TEXT"), (2, "word " * 500, "OCR")])
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["ocr_pages"], 1)
        self.assertGreater(stats["words"], 500)
        self.assertEqual(stats["reading_minutes"], 3)


class QuestionChoiceTests(SimpleTestCase):
    def test_unanswerable_questions_are_dropped_and_sources_vary(self):
        candidates = [
            Candidate("When is A due?", (1, 0), "deadline"),
            Candidate("When is B due?", (1, 5), "deadline"),
            Candidate("Who issued this circular?", (1, 9), "field"),
            Candidate("What does the document say about nothing?", None, "phrase"),
        ]
        chosen, checks = choose(candidates, lambda q, a: "nothing" not in q)
        self.assertEqual(
            chosen, ["When is A due?", "Who issued this circular?", "When is B due?"]
        )
        self.assertEqual(checks, 4)


@override_settings(DOCUMENT_INSIGHTS_ENABLED=True)
class EnrichmentTests(IngestionTestCase):
    def upload_text(self, text, title="Transport circular", name="c.txt"):
        response = self.upload(name, text.encode(), "text/plain", title=title)
        self.assertEqual(response.status_code, 202)
        return Document.objects.get(pk=response.data["id"])

    def test_a_published_document_gets_its_brief(self):
        document = self.upload_text(CIRCULAR)
        content = document.current_version.current_content
        insight = DocumentInsight.objects.get(document_content=content)

        self.assertEqual(insight.status, DocumentInsight.Status.READY)
        self.assertEqual(len(insight.summary), 3)
        page_text = content.pages.get(page_number=1).text
        for item in insight.summary + insight.key_points:
            self.assertEqual(page_text[item["start"] : item["end"]], item["text"])
        self.assertTrue(insight.suggested_questions)
        self.assertIn(
            "What is the last date for renewal of the bus pass?",
            insight.suggested_questions,
        )
        self.assertEqual(insight.stats["language"], "en")
        deadline = DocumentEntity.objects.get(insight=insight, entity_type="DEADLINE")
        self.assertEqual(deadline.value_date, datetime.date(2027, 3, 15))

    def test_enrichment_is_on_the_timeline_after_indexing(self):
        document = self.upload_text(CIRCULAR)
        job = ProcessingJob.objects.get(document_version=document.current_version)
        stages = list(job.events.values_list("stage", flat=True))
        self.assertEqual(stages[-1], ProcessingJob.Stage.INSIGHTS)
        event = job.events.get(stage=ProcessingJob.Stage.INSIGHTS)
        self.assertEqual(event.status, ProcessingEvent.Status.SUCCEEDED)
        self.assertTrue(event.message.startswith("Brief ready"))
        self.assertEqual(event.metadata["steps"]["thumbnail"], "not_applicable")

    def test_a_centroid_is_stored_for_related_documents(self):
        document = self.upload_text(CIRCULAR)
        centroid = DocumentCentroid.objects.get(
            document_content=document.current_version.current_content
        )
        # The suite's hashing baseline is lexical, and the centroid says so.
        self.assertFalse(centroid.semantic)
        self.assertGreater(centroid.chunk_count, 0)

    def test_enrichment_is_idempotent(self):
        document = self.upload_text(CIRCULAR)
        version = document.current_version
        entities = DocumentEntity.objects.count()
        for _ in range(2):
            enrich_version(version.pk)
        self.assertEqual(DocumentInsight.objects.count(), 1)
        self.assertEqual(DocumentCentroid.objects.count(), 1)
        self.assertEqual(DocumentEntity.objects.count(), entities)

    def test_a_redelivered_task_updates_its_own_event(self):
        document = self.upload_text(CIRCULAR)
        version = document.current_version
        job = ProcessingJob.objects.get(document_version=version)
        enrich_version(version.pk, task_id="task-1")
        enrich_version(version.pk, task_id="task-1")
        self.assertEqual(
            job.events.filter(
                stage=ProcessingJob.Stage.INSIGHTS, metadata__task_id="task-1"
            ).count(),
            1,
        )

    def test_a_superseded_version_is_not_enriched(self):
        document = self.upload_text(CIRCULAR)
        old = document.current_version
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("document-versions", args=[document.pk]),
                {"file": SimpleUploadedFile("v2.txt", b"Second version text here.")},
                format="multipart",
            )
        self.assertEqual(enrich_version(old.pk), {"skipped": "superseded"})

    def test_a_failing_brief_never_fails_the_document(self):
        with mock.patch.object(
            insight_service, "_build", side_effect=RuntimeError("defect")
        ):
            document = self.upload_text(CIRCULAR)

        self.assertEqual(document.status, Document.Status.COMPLETED)
        insight = DocumentInsight.objects.get(
            document_content=document.current_version.current_content
        )
        self.assertEqual(insight.status, DocumentInsight.Status.FAILED)
        event = ProcessingEvent.objects.get(stage=ProcessingJob.Stage.INSIGHTS)
        self.assertEqual(event.status, ProcessingEvent.Status.FAILED)

    def test_a_failed_regeneration_keeps_the_good_brief(self):
        document = self.upload_text(CIRCULAR)
        version = document.current_version
        with mock.patch.object(
            insight_service, "_build", side_effect=RuntimeError("defect")
        ):
            enrich_version(version.pk)
        insight = DocumentInsight.objects.get(document_content=version.current_content)
        self.assertEqual(insight.status, DocumentInsight.Status.READY)

    @override_settings(DOCUMENT_INSIGHTS_ENABLED=False)
    def test_enrichment_can_be_switched_off(self):
        self.upload_text(CIRCULAR)
        self.assertFalse(DocumentInsight.objects.exists())
        self.assertFalse(
            ProcessingEvent.objects.filter(stage=ProcessingJob.Stage.INSIGHTS).exists()
        )

    def test_a_pdf_gets_a_first_page_thumbnail(self):
        response = self.upload(
            "c.pdf",
            make_pdf_bytes(["Circular page one", "Page two"]),
            "application/pdf",
        )
        document = Document.objects.get(pk=response.data["id"])
        thumbnail = DocumentThumbnail.objects.get(
            document_version=document.current_version
        )
        self.assertIn(thumbnail.mime_type, ("image/webp", "image/png"))
        self.assertLessEqual(thumbnail.width, 320)
        self.assertTrue(thumbnail.file.storage.exists(thumbnail.file.name))

    def test_deleting_a_document_removes_its_thumbnail_file(self):
        response = self.upload("c.pdf", make_pdf_bytes(["Page one"]), "application/pdf")
        document = Document.objects.get(pk=response.data["id"])
        thumbnail = DocumentThumbnail.objects.get(
            document_version=document.current_version
        )
        name, storage = thumbnail.file.name, thumbnail.file.storage
        with self.captureOnCommitCallbacks(execute=True):
            document.delete()
        self.assertFalse(storage.exists(name))

    def test_fields_are_re_extracted_when_the_type_no_longer_matches(self):
        document = self.upload_text(CIRCULAR)
        content = document.current_version.current_content
        self.assertEqual(
            ExtractionResult.objects.filter(document_content=content).count(), 1
        )
        Document.objects.filter(pk=document.pk).update(
            document_type="POLICY", document_type_source="user"
        )
        enrich_version(document.current_version.pk, reason="type_changed")
        newest = ExtractionResult.objects.filter(document_content=content).first()
        self.assertEqual(newest.document_type, "POLICY")
        # Again: converged, nothing new.
        enrich_version(document.current_version.pk, reason="type_changed")
        self.assertEqual(
            ExtractionResult.objects.filter(document_content=content).count(), 2
        )


@override_settings(DOCUMENT_INSIGHTS_ENABLED=True)
class BuildInsightsCommandTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        response = self.upload("c.txt", CIRCULAR.encode(), "text/plain")
        self.document = Document.objects.get(pk=response.data["id"])
        DocumentInsight.objects.all().delete()

    def call(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("build_insights", *args, stdout=out)
        return out.getvalue()

    def test_missing_briefs_are_built(self):
        output = self.call("--missing", "--sync")
        self.assertIn("Enriched 1 inline", output)
        self.assertEqual(DocumentInsight.objects.count(), 1)
        # Nothing is missing any more.
        self.assertIn("Selected : 0", self.call("--missing", "--dry-run"))

    def test_a_dry_run_builds_nothing(self):
        self.assertIn("Selected : 1", self.call("--all", "--dry-run"))
        self.assertFalse(DocumentInsight.objects.exists())

    def test_one_document(self):
        self.call("--document", str(self.document.pk), "--sync")
        self.assertEqual(DocumentInsight.objects.count(), 1)

    def test_an_unknown_document_is_an_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.call("--document", "999999", "--sync")
