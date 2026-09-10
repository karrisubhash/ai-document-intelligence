import dataclasses

from django.test import SimpleTestCase, override_settings

from documents.models import ClassificationResult, ProcessingEvent, ProcessingJob
from documents.processing.classification import (
    UNKNOWN_LABEL,
    ClassificationPrediction,
    DocumentClassifier,
    classify,
    get_classifier,
    reset_classifier_cache,
)
from documents.processing.classification.exceptions import (
    ClassificationError,
    ClassifierNotConfigured,
    ClassifierUnavailable,
)
from documents.processing.errors import ErrorCode
from documents.tests_ingestion import IngestionTestCase, make_pdf_bytes

# --- Test doubles -----------------------------------------------------------
# Deliberately fixed outputs. These make no attempt to actually classify
# anything - no keyword matching, no heuristics. They exist only to prove the
# integration boundary works, and are never referenced by production settings.


class StubClassifier:
    name = "stub-classifier"
    version = "test-v0"

    def predict(self, *, document_content_id):
        return ClassificationPrediction(
            label="letter",
            confidence=0.5,
            model_name=self.name,
            model_version=self.version,
            input_reference=f"document_content:{document_content_id}",
        )


class UnknownLabelClassifier(StubClassifier):
    def predict(self, *, document_content_id):
        return dataclasses.replace(
            super().predict(document_content_id=document_content_id),
            label=UNKNOWN_LABEL,
            confidence=0.2,
        )


class OutOfRangeConfidenceClassifier(StubClassifier):
    def predict(self, *, document_content_id):
        return dataclasses.replace(
            super().predict(document_content_id=document_content_id),
            confidence=4.2,
        )


class EmptyLabelClassifier(StubClassifier):
    def predict(self, *, document_content_id):
        return dataclasses.replace(
            super().predict(document_content_id=document_content_id),
            label="",
        )


class UnavailableClassifier(StubClassifier):
    def predict(self, *, document_content_id):
        raise ClassifierUnavailable("model server unreachable")


STUB = "documents.tests_classification.StubClassifier"
UNKNOWN = "documents.tests_classification.UnknownLabelClassifier"
BAD_CONFIDENCE = "documents.tests_classification.OutOfRangeConfidenceClassifier"
EMPTY_LABEL = "documents.tests_classification.EmptyLabelClassifier"
UNAVAILABLE = "documents.tests_classification.UnavailableClassifier"


class ClassificationContractTests(SimpleTestCase):
    def test_prediction_is_immutable(self):
        prediction = StubClassifier().predict(document_content_id=1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            prediction.label = "invoice"

    def test_stub_satisfies_the_classifier_protocol(self):
        self.assertIsInstance(StubClassifier(), DocumentClassifier)

    def test_unknown_label_is_recognisable(self):
        self.assertTrue(UnknownLabelClassifier().predict(document_content_id=1).is_unknown)
        self.assertFalse(StubClassifier().predict(document_content_id=1).is_unknown)


class ClassifierResolutionTests(SimpleTestCase):
    def setUp(self):
        reset_classifier_cache()
        self.addCleanup(reset_classifier_cache)

    @override_settings(DOCUMENT_CLASSIFIER="")
    def test_unconfigured_refuses_rather_than_guessing(self):
        with self.assertRaises(ClassifierNotConfigured):
            get_classifier()

    @override_settings(DOCUMENT_CLASSIFIER=STUB)
    def test_configured_classifier_is_loaded_once(self):
        self.assertIs(get_classifier(), get_classifier())


class ClassificationServiceTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        reset_classifier_cache()
        self.addCleanup(reset_classifier_cache)

    def upload_and_get_content(self):
        """Upload with no classifier configured, so the pipeline's own
        CLASSIFICATION stage skips and these tests exercise the service
        directly rather than through the stage."""
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Some document body"]), "application/pdf"
        )
        return self.content_for(response.data["id"])

    def test_persists_result_against_version_content_and_job(self):
        content = self.upload_and_get_content()

        with override_settings(DOCUMENT_CLASSIFIER=STUB):
            result = classify(content.id, processing_job=content.processing_job)

        self.assertEqual(result.document_content_id, content.id)
        self.assertEqual(result.document_version_id, content.document_version_id)
        self.assertEqual(result.processing_job_id, content.processing_job_id)
        self.assertEqual(result.model_name, "stub-classifier")
        self.assertEqual(result.model_version, "test-v0")
        self.assertEqual(result.predicted_label, "letter")
        self.assertEqual(result.confidence, 0.5)
        self.assertEqual(result.input_reference, f"document_content:{content.id}")

    def test_rejects_confidence_outside_zero_to_one(self):
        content = self.upload_and_get_content()

        with override_settings(DOCUMENT_CLASSIFIER=BAD_CONFIDENCE):
            with self.assertRaises(ClassificationError):
                classify(content.id)

        self.assertFalse(ClassificationResult.objects.exists())

    def test_rejects_empty_label(self):
        content = self.upload_and_get_content()

        with override_settings(DOCUMENT_CLASSIFIER=EMPTY_LABEL):
            with self.assertRaises(ClassificationError):
                classify(content.id)

        self.assertFalse(ClassificationResult.objects.exists())

    def test_unknown_is_a_storable_verdict(self):
        content = self.upload_and_get_content()

        with override_settings(DOCUMENT_CLASSIFIER=UNKNOWN):
            result = classify(content.id)

        self.assertEqual(result.predicted_label, UNKNOWN_LABEL)
        self.assertEqual(result.confidence, 0.2)

    def test_unavailable_classifier_is_reported_as_transient(self):
        content = self.upload_and_get_content()

        with override_settings(DOCUMENT_CLASSIFIER=UNAVAILABLE):
            with self.assertRaises(ClassifierUnavailable):
                classify(content.id)


class ClassificationStageTests(IngestionTestCase):
    """The CLASSIFICATION stage's behavior inside the real pipeline."""

    def setUp(self):
        super().setUp()
        reset_classifier_cache()
        self.addCleanup(reset_classifier_cache)

    def upload_pdf(self):
        return self.upload(
            "doc.pdf", make_pdf_bytes(["Document body text"]), "application/pdf"
        )

    def classification_event(self, document_id):
        return self.job_for(document_id).events.get(
            stage=ProcessingJob.Stage.CLASSIFICATION
        )

    def test_stage_is_skipped_when_no_classifier_is_configured(self):
        response = self.upload_pdf()
        event = self.classification_event(response.data["id"])

        self.assertEqual(event.status, ProcessingEvent.Status.SKIPPED)
        self.assertFalse(ClassificationResult.objects.exists())
        # A skipped classification must not fail the document.
        self.assertEqual(
            self.job_for(response.data["id"]).status, ProcessingJob.Status.COMPLETED
        )

    @override_settings(DOCUMENT_CLASSIFIER=STUB)
    def test_stage_records_a_result_when_a_classifier_is_configured(self):
        response = self.upload_pdf()
        event = self.classification_event(response.data["id"])

        self.assertEqual(event.status, ProcessingEvent.Status.SUCCEEDED)
        self.assertEqual(event.metadata["predicted_label"], "letter")
        self.assertEqual(event.metadata["model_version"], "test-v0")

        result = ClassificationResult.objects.get()
        self.assertEqual(result.id, event.metadata["classification_result_id"])

    @override_settings(DOCUMENT_CLASSIFIER=STUB)
    def test_classification_consumes_persisted_content_not_the_raw_file(self):
        response = self.upload_pdf()
        content = self.content_for(response.data["id"])
        result = ClassificationResult.objects.get()

        # The stage was handed the DocumentContent id, proving it read the
        # normalized representation rather than reopening the PDF.
        self.assertEqual(result.input_reference, f"document_content:{content.id}")
        self.assertEqual(result.document_content_id, content.id)

    @override_settings(DOCUMENT_CLASSIFIER=BAD_CONFIDENCE)
    def test_classifier_error_fails_the_job_permanently(self):
        response = self.upload_expecting_failure(
            "doc.pdf", make_pdf_bytes(["Body"]), "application/pdf"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(job.error_code, ErrorCode.CLASSIFICATION_FAILED)
        self.assertEqual(job.attempt, 1)

    @override_settings(DOCUMENT_CLASSIFIER=STUB)
    def test_content_still_persists_even_though_classification_runs_after(self):
        response = self.upload_pdf()
        content = self.content_for(response.data["id"])

        self.assertEqual(content.page_count, 1)
        self.assertIn("Document body", content.pages.first().text)
