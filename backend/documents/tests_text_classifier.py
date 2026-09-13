"""Tests for the trained text classifier's inference adapter.

These build their own tiny model artifact rather than depending on the
real one: `ai/classification/artifacts/` is git-ignored, so a suite that
needed a trained model would pass only on machines that had trained it.
What is under test here is the adapter and the artifact contract, not
the accuracy of any particular model.
"""

import importlib.util
import unittest

from django.test import SimpleTestCase, override_settings

from documents.models import ClassificationResult, DocumentContent, PageContent
from documents.processing.classification import (
    UNKNOWN_LABEL,
    DocumentClassifier,
    classify,
    reset_classifier_cache,
)
from documents.processing.classification.exceptions import (
    ClassificationError,
    ClassifierUnavailable,
)
from documents.processing.classification.text_classifier import (
    TextDocumentClassifier,
    normalize,
)
from documents.tests_ingestion import IngestionTestCase

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None
requires_sklearn = unittest.skipUnless(
    HAS_SKLEARN, "scikit-learn not installed (backend/requirements-ml.txt)"
)

ADAPTER = "documents.processing.classification.text_classifier.TextDocumentClassifier"

DEFAULT_PREPROCESSING = {
    "unicode_normalization": "NFKC",
    "lowercase": True,
    "collapse_whitespace": True,
}


def build_artifact(path, *, threshold=0.0, preprocessing=None, drop_field=None):
    """Fit a throwaway 3-class model and write it in artifact format."""
    import joblib
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    model = Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=False)),
            ("classifier", LogisticRegression(max_iter=1000)),
        ]
    )
    model.fit(
        ["invoice total due amount"] * 3
        + ["dear sir yours sincerely letter"] * 3
        + ["memorandum to all staff"] * 3,
        ["invoice"] * 3 + ["letter"] * 3 + ["memo"] * 3,
    )

    artifact = {
        "model": model,
        "labels": ["invoice", "letter", "memo"],
        "unknown_label": UNKNOWN_LABEL,
        "threshold": threshold,
        "preprocessing": preprocessing or DEFAULT_PREPROCESSING,
        "model_name": "test-text-classifier",
        "model_version": "0.0.1",
    }
    artifact.pop(drop_field, None)

    joblib.dump(artifact, path)
    return str(path)


# --- Normalization ----------------------------------------------------------


class NormalizationEquivalenceTests(SimpleTestCase):
    """The adapter reimplements the training-side normalization rather
    than importing it, to keep Django off the `ai` package. These pin the
    *same literal outputs* asserted in
    ai/classification/tests/test_text_baseline.py, so if either side
    changes without the other, one of the two suites fails.
    """

    def test_folds_ligatures_lowercases_and_collapses(self):
        self.assertEqual(
            normalize("  ﬁLE   Folder\n\n", DEFAULT_PREPROCESSING),
            "file folder",
        )

    def test_keeps_numbers_and_punctuation(self):
        self.assertEqual(
            normalize("Invoice #INV-2024/017: $1,299.00", DEFAULT_PREPROCESSING),
            "invoice #inv-2024/017: $1,299.00",
        )

    def test_empty_document_stays_empty(self):
        self.assertEqual(normalize("   \n\t ", DEFAULT_PREPROCESSING), "")

    def test_each_step_can_be_turned_off(self):
        self.assertEqual(
            normalize(
                "  A  B ",
                {
                    "unicode_normalization": None,
                    "lowercase": False,
                    "collapse_whitespace": False,
                },
            ),
            "  A  B ",
        )

    def test_refuses_preprocessing_it_does_not_implement(self):
        """An artifact asking for a step this adapter lacks must fail:
        serving text shaped differently from training is the failure
        mode this whole parameter-passing arrangement exists to stop."""
        with self.assertRaises(ClassificationError) as caught:
            normalize("x", {"stem_words": True})

        self.assertIn("stem_words", str(caught.exception))

    def test_refuses_unknown_unicode_form(self):
        with self.assertRaises(ClassificationError):
            normalize("x", {"unicode_normalization": "NFKZ"})


# --- Artifact loading -------------------------------------------------------


@requires_sklearn
class ArtifactLoadingTests(SimpleTestCase):
    def test_missing_artifact_is_unavailable_not_a_crash(self):
        with self.assertRaises(ClassifierUnavailable) as caught:
            TextDocumentClassifier("no/such/model.joblib")

        self.assertIn("train_text", str(caught.exception))

    def test_unreadable_artifact_is_unavailable(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.joblib"
            path.write_bytes(b"this is not a joblib file")

            with self.assertRaises(ClassifierUnavailable):
                TextDocumentClassifier(str(path))

    def test_artifact_missing_a_required_field_is_unavailable(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.joblib"
            build_artifact(path, drop_field="threshold")

            with self.assertRaises(ClassifierUnavailable) as caught:
                TextDocumentClassifier(str(path))

            self.assertIn("threshold", str(caught.exception))

    def test_satisfies_the_classifier_protocol(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.joblib"
            build_artifact(path)

            self.assertIsInstance(TextDocumentClassifier(str(path)), DocumentClassifier)


# --- Prediction -------------------------------------------------------------


@requires_sklearn
class PredictionTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        reset_classifier_cache()
        self.addCleanup(reset_classifier_cache)

        import tempfile
        from pathlib import Path

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.artifact = Path(self.directory.name) / "model.joblib"

        # One real upload gives a DocumentVersion to hang content on;
        # the pages under test are then written directly, so each test
        # controls exactly what the model is asked to read.
        response = self.upload("doc.txt", b"placeholder", "text/plain")
        self.version = self.content_for(response.data["id"]).document_version

    def make_content(self, page_texts):
        content = DocumentContent.objects.create(
            document_version=self.version,
            processing_version="test",
            page_count=len(page_texts),
        )
        PageContent.objects.bulk_create(
            [
                PageContent(
                    content=content,
                    page_number=number,
                    text=text,
                    extraction_method=PageContent.ExtractionMethod.PLAIN_TEXT,
                    character_count=len(text),
                )
                for number, text in enumerate(page_texts, start=1)
            ]
        )
        return content

    def test_predicts_a_label_and_records_the_score_distribution(self):
        build_artifact(self.artifact, threshold=0.0)
        classifier = TextDocumentClassifier(str(self.artifact))

        prediction = classifier.predict(
            document_content_id=self.make_content(["invoice total due amount"]).id
        )

        self.assertEqual(prediction.label, "invoice")
        self.assertEqual(prediction.model_name, "test-text-classifier")
        self.assertTrue(0.0 <= prediction.confidence <= 1.0)
        self.assertEqual(
            sorted(prediction.metadata["scores"]), ["invoice", "letter", "memo"]
        )
        self.assertEqual(prediction.metadata["pages_scored"], 1)

    def test_abstains_below_the_threshold_but_records_what_it_would_have_said(self):
        # No 3-class average clears 0.99, so this always abstains.
        build_artifact(self.artifact, threshold=0.99)
        classifier = TextDocumentClassifier(str(self.artifact))

        prediction = classifier.predict(
            document_content_id=self.make_content(["invoice total due amount"]).id
        )

        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertTrue(prediction.is_unknown)
        self.assertTrue(prediction.metadata["abstained"])
        self.assertEqual(prediction.metadata["top_label"], "invoice")

    def test_scores_every_page_that_has_text(self):
        build_artifact(self.artifact, threshold=0.0)
        classifier = TextDocumentClassifier(str(self.artifact))

        content = self.make_content(
            ["invoice total due amount", "", "   ", "invoice amount due"]
        )
        prediction = classifier.predict(document_content_id=content.id)

        # Blank pages carry no evidence and must not dilute the average.
        self.assertEqual(prediction.metadata["pages_total"], 4)
        self.assertEqual(prediction.metadata["pages_scored"], 2)
        self.assertEqual(prediction.metadata["scored_page_numbers"], [1, 4])

    def test_document_with_no_text_abstains_rather_than_guessing(self):
        build_artifact(self.artifact, threshold=0.0)
        classifier = TextDocumentClassifier(str(self.artifact))

        prediction = classifier.predict(
            document_content_id=self.make_content(["", "  "]).id
        )

        self.assertEqual(prediction.label, UNKNOWN_LABEL)
        self.assertEqual(prediction.confidence, 0.0)
        self.assertEqual(prediction.metadata["reason"], "no_text")

    def test_metadata_reaches_the_persisted_result(self):
        """The point of the metadata field: a stored prediction can be
        examined afterwards, not just believed."""
        build_artifact(self.artifact, threshold=0.0)
        content = self.make_content(["dear sir yours sincerely letter"])

        with override_settings(
            DOCUMENT_CLASSIFIER=ADAPTER,
            DOCUMENT_CLASSIFIER_ARTIFACT=str(self.artifact),
        ):
            result = classify(content.id)

        stored = ClassificationResult.objects.get(id=result.id)
        self.assertEqual(stored.predicted_label, "letter")
        self.assertEqual(stored.model_name, "test-text-classifier")
        self.assertIn("scores", stored.metadata)
        self.assertEqual(stored.metadata["pages_scored"], 1)
        self.assertEqual(stored.input_reference, f"document_content:{content.id}")
