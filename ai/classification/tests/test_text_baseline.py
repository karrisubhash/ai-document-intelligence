import tempfile
import unittest
from pathlib import Path

from ai.classification.preprocessing import text as preprocessing
from ai.classification.training.train_text import select_threshold


class NormalizationTests(unittest.TestCase):
    def test_applies_steps_in_the_documented_order(self):
        # NFKC folds the ligature to "fi", then lowercase, then collapse.
        self.assertEqual(
            preprocessing.normalize("  ﬁLE   Folder\n\n"),
            "file folder",
        )

    def test_keeps_numbers_and_punctuation(self):
        """Invoice numbers, dates and amounts are signal, not noise."""
        self.assertEqual(
            preprocessing.normalize("Invoice #INV-2024/017: $1,299.00"),
            "invoice #inv-2024/017: $1,299.00",
        )

    def test_each_step_can_be_turned_off(self):
        self.assertEqual(
            preprocessing.normalize(
                "  A  B ",
                unicode_normalization=None,
                lowercase=False,
                collapse_whitespace=False,
            ),
            "  A  B ",
        )

    def test_empty_document_stays_empty(self):
        """2.42% of QS-OCR has no text; it must survive as an empty
        string rather than becoming a sentinel or being dropped."""
        self.assertEqual(preprocessing.normalize("   \n\t "), "")

    def test_rejects_unknown_unicode_form(self):
        with self.assertRaises(ValueError):
            preprocessing.normalize("x", unicode_normalization="NFKZ")


class ParameterContractTests(unittest.TestCase):
    """`apply` is the function the Django adapter calls with parameters
    read out of a model artifact. It has to refuse anything it does not
    implement, or serving would silently diverge from training."""

    def test_applies_artifact_parameters(self):
        self.assertEqual(
            preprocessing.apply("  Hello   WORLD ", {"lowercase": False}),
            "Hello WORLD",
        )

    def test_rejects_unsupported_parameter(self):
        with self.assertRaises(ValueError) as caught:
            preprocessing.apply("x", {"stem_words": True})

        self.assertIn("stem_words", str(caught.exception))

    def test_defaults_match_the_committed_config(self):
        self.assertEqual(
            preprocessing.DEFAULT_PARAMETERS,
            {
                "unicode_normalization": "NFKC",
                "lowercase": True,
                "collapse_whitespace": True,
            },
        )


class ReadDocumentTests(unittest.TestCase):
    def test_missing_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            preprocessing.read_document("does/not/exist.txt")

    def test_empty_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.txt"
            path.write_text("", encoding="utf-8")

            self.assertEqual(preprocessing.read_document(path), "")

    def test_accepts_windows_separators_in_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / "a"
            nested.mkdir()
            (nested / "doc.txt").write_text("text", encoding="utf-8")

            windows_style = f"{directory}\\a\\doc.txt"
            self.assertEqual(preprocessing.read_document(windows_style), "text")


class ThresholdSelectionTests(unittest.TestCase):
    """The abstention threshold is fitted, so the fitting rule itself is
    what needs pinning down."""

    def test_picks_lowest_threshold_that_reaches_the_target(self):
        sweep = [
            {
                "threshold": 0.0,
                "coverage": 1.0,
                "answered": 100,
                "accuracy_when_answering": 0.70,
            },
            {
                "threshold": 0.5,
                "coverage": 0.6,
                "answered": 60,
                "accuracy_when_answering": 0.91,
            },
            {
                "threshold": 0.9,
                "coverage": 0.2,
                "answered": 20,
                "accuracy_when_answering": 0.99,
            },
        ]

        selection = select_threshold(sweep, 0.90)

        # 0.9 is more accurate, but 0.5 already clears the bar and keeps
        # three times the coverage.
        self.assertEqual(selection["threshold"], 0.5)
        self.assertTrue(selection["target_met"])

    def test_reports_shortfall_rather_than_pretending(self):
        sweep = [
            {
                "threshold": 0.0,
                "coverage": 1.0,
                "answered": 100,
                "accuracy_when_answering": 0.50,
            },
            {
                "threshold": 0.5,
                "coverage": 0.4,
                "answered": 40,
                "accuracy_when_answering": 0.75,
            },
        ]

        selection = select_threshold(sweep, 0.90)

        self.assertFalse(selection["target_met"])
        self.assertEqual(selection["threshold"], 0.5)
        self.assertEqual(selection["target_accuracy"], 0.90)

    def test_ignores_thresholds_that_answer_nothing(self):
        """A cutoff that abstains on everything has undefined accuracy
        and must never be selected as though it were perfect."""
        sweep = [
            {
                "threshold": 0.5,
                "coverage": 0.5,
                "answered": 50,
                "accuracy_when_answering": 0.92,
            },
            {
                "threshold": 0.99,
                "coverage": 0.0,
                "answered": 0,
                "accuracy_when_answering": 0.0,
            },
        ]

        self.assertEqual(select_threshold(sweep, 0.90)["threshold"], 0.5)


if __name__ == "__main__":
    unittest.main()
