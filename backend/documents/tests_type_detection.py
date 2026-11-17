"""Institutional type detection: the detector, the stage, and who decides.

The detector tests call `detect()` on plain (page_number, text) pairs - no
database - so the rules can be checked one at a time. The pipeline tests
upload real files and check what the document ends up typed as, and what
was recorded about why.
"""

from django.test import SimpleTestCase, override_settings

from documents.models import (
    ClassificationResult,
    Document,
    ExtractionResult,
    ProcessingJob,
)
from documents.processing.chunking import reset_provider_cache
from documents.processing.classification import (
    UNKNOWN_LABEL,
    TypeOutcome,
    apply_document_type,
    reset_classifier_cache,
)
from documents.processing.classification.institutional import (
    InstitutionalTypeDetector,
)
from documents.processing.embedding.hashing import HashingEmbeddingProvider
from documents.tests_ingestion import IngestionTestCase

CIRCULAR = """CIRCULAR
Office of the Registrar
Circular No: TR/2024/117
Date: 12/03/2024
Subject: Campus bus services for the academic year 2024-25

All students are hereby informed that the bus pass costs 4500 rupees per
semester. This circular supersedes circular TR/2023/094.
"""

SYLLABUS = """Course Syllabus
Course Code: CS301
Course Title: Operating Systems
Credits: 4
Semester: V

Course Objectives
To understand process management and memory management.

Unit I Introduction
Unit II Processes and threads
Text Books
1. Silberschatz, Operating System Concepts.
"""

FORM = """APPLICATION FORM FOR HOSTEL ADMISSION
Name of the applicant: ______________________
Roll No: ______________________
Department: ______________________
I hereby declare that the particulars given above are true.
Signature of the applicant ______________________
For office use only
"""

POLICY = """INFORMATION TECHNOLOGY ACCEPTABLE USE POLICY
Computer Centre

Clause 1.1 Accounts
Every student is issued one institute account.
"""

NOTES = """THEORY OF MACHINES - UNIT 4 LECTURE NOTES
Brakes, Clutches and Dynamometers
Department of Mechanical Engineering - course notes, not a regulation

4.1 Introduction
A brake is a device by which artificial frictional resistance is applied.
"""


class SemanticHashingProvider(HashingEmbeddingProvider):
    """The hashing baseline, declared semantic - a stand-in for a real model.

    Its vectors measure vocabulary overlap, which is enough to test that
    the semantic signal is consulted, recorded and bounded.
    """

    name = "test-semantic"
    version = "0"
    semantic = True


SEMANTIC = f"{__name__}.SemanticHashingProvider"


def detect(text, title=""):
    return InstitutionalTypeDetector().detect(pages=[(1, text)], title=title)


class DetectorTests(SimpleTestCase):
    def test_a_circular_is_recognised_with_its_reasons(self):
        prediction = detect(CIRCULAR)

        self.assertEqual(prediction.label, "CIRCULAR")
        self.assertGreaterEqual(prediction.confidence, 0.9)
        reasons = prediction.metadata["evidence"]["CIRCULAR"]
        self.assertIn("the heading calls it a circular", reasons)
        self.assertIn("circular number found", reasons)
        self.assertFalse(prediction.metadata["abstained"])

    def test_a_syllabus_is_recognised(self):
        prediction = detect(SYLLABUS)
        self.assertEqual(prediction.label, "SYLLABUS")
        self.assertIn("course code found", prediction.metadata["evidence"]["SYLLABUS"])

    def test_a_form_is_recognised(self):
        self.assertEqual(detect(FORM).label, "FORM")

    def test_a_heading_that_names_the_type_is_enough(self):
        self.assertEqual(detect(POLICY).label, "POLICY")

    def test_the_title_counts_as_the_heading(self):
        prediction = detect("Every student is issued one account.", title="IT Policy")
        self.assertEqual(prediction.label, "POLICY")

    def test_a_negated_mention_is_not_a_heading(self):
        prediction = detect(NOTES)
        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertNotIn(
            "the heading calls it regulations",
            prediction.metadata["evidence"].get("REGULATION", []),
        )

    def test_one_weak_cue_is_not_enough(self):
        prediction = detect("Subject: annual sports day\nThe event is on Friday.")
        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertTrue(prediction.metadata["abstained"])
        # The best guess is still on record, with every score.
        self.assertEqual(prediction.metadata["top_label"], "CIRCULAR")
        self.assertEqual(
            set(prediction.metadata["scores"]),
            {"CIRCULAR", "REGULATION", "POLICY", "SYLLABUS", "FORM"},
        )

    def test_an_empty_document_abstains(self):
        prediction = detect("")
        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertLess(prediction.confidence, 0.2)

    def test_a_table_of_many_rows_lends_no_field_as_evidence(self):
        """Labelled table rows ("Date: 12/03/2025" on every row of an exam
        timetable) are read as field extraction reads them: a value that
        recurs row after row is no field of the document, so it votes for
        no type. A form's one-per-field rows still do."""
        rows = "\n".join(
            f"Table 1, row {n}: Date: {n:02d}/03/2025; Subject: Paper {n}; Hall: H{n}"
            for n in range(1, 7)
        )
        evidence = detect(rows).metadata.get("evidence", {})
        self.assertNotIn("issue date found", evidence.get("CIRCULAR", []))

        form = "Table 1, row 1: Circular No: ACAD/2025/14\nTable 1, row 2: Date: 12/03/2025"
        evidence = detect(form).metadata.get("evidence", {})
        self.assertIn("issue date found", evidence.get("CIRCULAR", []))

    def test_general_and_other_are_never_predicted(self):
        for text in (CIRCULAR, SYLLABUS, FORM, POLICY, NOTES, ""):
            self.assertNotIn(detect(text).label, ("GENERAL", "OTHER"))

    @override_settings(DOCUMENT_TYPE_MIN_CONFIDENCE=0.9)
    def test_the_threshold_is_configurable(self):
        # A heading alone gives about 65% - enough by default, not at 90%.
        prediction = detect(POLICY)
        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertEqual(prediction.metadata["top_label"], "POLICY")
        self.assertEqual(prediction.metadata["threshold"], 0.9)

    def test_the_test_configuration_uses_patterns_only(self):
        # The suite pins the hashing baseline, which is not semantic.
        self.assertEqual(detect(CIRCULAR).metadata["signals"], ["patterns"])

    def test_confidence_is_a_probability(self):
        for text in (CIRCULAR, SYLLABUS, FORM, POLICY, NOTES, ""):
            prediction = detect(text)
            self.assertGreaterEqual(prediction.confidence, 0.0)
            self.assertLessEqual(prediction.confidence, 1.0)
            total = (
                sum(prediction.metadata["scores"].values())
                + prediction.metadata["none"]
            )
            self.assertAlmostEqual(total, 1.0, places=3)


@override_settings(DOCUMENT_EMBEDDING_PROVIDER=SEMANTIC)
class SemanticSignalTests(SimpleTestCase):
    def setUp(self):
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)

    def test_a_semantic_provider_adds_prototype_similarity(self):
        prediction = detect(SYLLABUS)
        self.assertEqual(prediction.metadata["signals"], ["patterns", "embeddings"])
        self.assertEqual(
            set(prediction.metadata["semantic"]["cosine"]),
            {"CIRCULAR", "REGULATION", "POLICY", "SYLLABUS", "FORM"},
        )
        self.assertEqual(prediction.label, "SYLLABUS")

    def test_similarity_alone_never_decides_a_type(self):
        # Close to the syllabus prototypes in wording, but no pattern fires.
        text = (
            "Introduction: the topics covered in this course and the list of "
            "experiments, with the units of study and the reference material."
        )
        prediction = detect(text)
        self.assertIn("embeddings", prediction.metadata["signals"])
        self.assertEqual(prediction.label, UNKNOWN_LABEL)


class TypeDetectionPipelineTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        reset_classifier_cache()
        self.addCleanup(reset_classifier_cache)

    def upload_text(self, text, title="Upload", **fields):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": title,
                    "file": SimpleUploadedFile(
                        "upload.txt", text.encode(), content_type="text/plain"
                    ),
                    **fields,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 202, response.data)
        return Document.objects.get(pk=response.data["id"])

    def verdict(self, document):
        return ClassificationResult.objects.get(
            document_version=document.current_version,
            taxonomy=ClassificationResult.Taxonomy.DOCUMENT_TYPE,
        )

    def test_a_default_upload_is_typed_and_its_fields_extracted(self):
        document = self.upload_text(CIRCULAR)

        self.assertEqual(document.document_type, "CIRCULAR")
        self.assertEqual(document.document_type_source, Document.TypeSource.AUTO)
        self.assertGreaterEqual(document.type_confidence, 0.9)

        # EXTRACTION ran after the type was set, under the circular schema.
        result = ExtractionResult.objects.get(document_version=document.current_version)
        self.assertEqual(result.document_type, "CIRCULAR")
        found = result.extracted_fields.filter(status="FOUND")
        self.assertIn("circular_number", {field.field_name for field in found})

    def test_the_verdict_and_its_outcome_are_on_the_timeline(self):
        document = self.upload_text(CIRCULAR)
        event = (
            ProcessingJob.objects.get(document_version=document.current_version)
            .events.filter(stage=ProcessingJob.Stage.CLASSIFICATION)
            .get()
        )
        self.assertEqual(
            event.metadata["document_type"]["outcome"], TypeOutcome.APPLIED
        )
        self.assertIn("Detected type Circular", event.message)
        self.assertIn("set automatically", event.message)

    def test_a_type_the_uploader_chose_is_never_overwritten(self):
        document = self.upload_text(CIRCULAR, document_type="POLICY")

        self.assertEqual(document.document_type, "POLICY")
        self.assertEqual(document.document_type_source, Document.TypeSource.USER)
        self.assertIsNone(document.type_confidence)
        verdict = self.verdict(document)
        self.assertEqual(verdict.predicted_label, "CIRCULAR")

    def test_an_unrecognised_document_stays_general(self):
        document = self.upload_text("Minutes of the sports committee meeting.")

        self.assertEqual(document.document_type, "GENERAL")
        self.assertEqual(document.document_type_source, Document.TypeSource.DEFAULT)
        verdict = self.verdict(document)
        self.assertTrue(verdict.abstained)
        self.assertEqual(verdict.predicted_label, UNKNOWN_LABEL)

    @override_settings(DOCUMENT_TYPE_AUTO_APPLY=False)
    def test_auto_apply_can_be_switched_off(self):
        document = self.upload_text(CIRCULAR)

        self.assertEqual(document.document_type, "GENERAL")
        self.assertEqual(self.verdict(document).predicted_label, "CIRCULAR")

    def test_a_new_version_the_detector_cannot_type_resets_its_own_decision(self):
        document = self.upload_text(CIRCULAR)
        self.assertEqual(document.document_type, "CIRCULAR")

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("document-versions", args=[document.pk]),
                {"file": SimpleUploadedFile("v2.txt", b"Minutes of the meeting.")},
                format="multipart",
            )

        document.refresh_from_db()
        self.assertEqual(document.document_type, "GENERAL")
        self.assertEqual(document.document_type_source, Document.TypeSource.DEFAULT)
        self.assertIsNone(document.type_confidence)

    def test_a_verdict_on_a_superseded_version_changes_nothing(self):
        document = self.upload_text("Minutes of the meeting.")
        old_version = document.current_version
        verdict = self.verdict(document)

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("document-versions", args=[document.pk]),
                {"file": SimpleUploadedFile("v2.txt", b"Minutes again.")},
                format="multipart",
            )

        verdict.abstained = False
        verdict.predicted_label = "FORM"
        self.assertEqual(
            apply_document_type(old_version, verdict), TypeOutcome.SUPERSEDED
        )
        document.refresh_from_db()
        self.assertEqual(document.document_type, "GENERAL")

    @override_settings(
        DOCUMENT_TYPE_DETECTOR="documents.tests_type_detection.BrokenDetector"
    )
    def test_a_failing_detector_never_fails_the_document(self):
        document = self.upload_text(CIRCULAR)

        self.assertEqual(document.status, Document.Status.COMPLETED)
        self.assertEqual(document.document_type, "GENERAL")
        event = (
            ProcessingJob.objects.get(document_version=document.current_version)
            .events.filter(stage=ProcessingJob.Stage.CLASSIFICATION)
            .get()
        )
        self.assertEqual(event.metadata["document_type"]["error"], "RuntimeError")


class BrokenDetector:
    name = "broken"
    version = "0"

    def predict(self, *, document_content_id):
        raise RuntimeError("defect")
