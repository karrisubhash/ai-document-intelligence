import unittest

from ai.classification.evaluation.metrics import evaluate, render, render_confusion

LABELS = ["invoice", "letter", "memo"]


class PerfectPredictionTests(unittest.TestCase):
    def test_all_metrics_are_one(self):
        truth = ["invoice", "letter", "memo", "invoice"]
        report = evaluate(truth, list(truth), LABELS)

        self.assertEqual(report.accuracy, 1.0)
        self.assertEqual(report.macro_f1, 1.0)
        self.assertEqual(report.weighted_f1, 1.0)
        self.assertEqual(report.confusion[0][0], 2)


class KnownValueTests(unittest.TestCase):
    """Hand-computed so the implementation is checked against arithmetic,
    not against itself."""

    def setUp(self):
        # invoice: 2 correct, 1 predicted as letter
        # letter : 1 correct, 1 predicted as memo
        # memo   : 1 correct
        self.truth = ["invoice"] * 3 + ["letter"] * 2 + ["memo"]
        self.pred = ["invoice", "invoice", "letter", "letter", "memo", "memo"]
        self.report = evaluate(self.truth, self.pred, LABELS)

    def test_accuracy(self):
        self.assertAlmostEqual(self.report.accuracy, 4 / 6)

    def test_invoice_precision_and_recall(self):
        invoice = next(c for c in self.report.per_class if c.label == "invoice")
        self.assertAlmostEqual(invoice.precision, 1.0)  # 2 of 2 predicted
        self.assertAlmostEqual(invoice.recall, 2 / 3)  # 2 of 3 actual
        self.assertAlmostEqual(invoice.f1, 2 * 1.0 * (2 / 3) / (1.0 + 2 / 3))

    def test_letter_precision_and_recall(self):
        letter = next(c for c in self.report.per_class if c.label == "letter")
        self.assertAlmostEqual(letter.precision, 1 / 2)  # 1 of 2 predicted
        self.assertAlmostEqual(letter.recall, 1 / 2)  # 1 of 2 actual

    def test_macro_f1_is_the_unweighted_mean(self):
        expected = sum(c.f1 for c in self.report.per_class) / len(LABELS)
        self.assertAlmostEqual(self.report.macro_f1, expected)

    def test_confusion_rows_are_actual_classes(self):
        self.assertEqual(self.report.confusion[0], [2, 1, 0])  # invoice
        self.assertEqual(self.report.confusion[1], [0, 1, 1])  # letter
        self.assertEqual(self.report.confusion[2], [0, 0, 1])  # memo

    def test_most_confused_ranks_off_diagonal_pairs(self):
        pairs = self.report.most_confused()
        self.assertTrue(all(p["actual"] != p["predicted"] for p in pairs))
        self.assertEqual(
            {(p["actual"], p["predicted"]) for p in pairs},
            {("invoice", "letter"), ("letter", "memo")},
        )


class MacroVersusWeightedTests(unittest.TestCase):
    def test_macro_punishes_a_failing_rare_class(self):
        # 20 easy invoices, 1 memo that is always missed. Accuracy stays
        # high; macro-F1 must not.
        truth = ["invoice"] * 20 + ["memo"]
        pred = ["invoice"] * 21

        report = evaluate(truth, pred, LABELS)

        self.assertAlmostEqual(report.accuracy, 20 / 21)
        self.assertLess(report.macro_f1, 0.5)
        self.assertGreater(report.weighted_f1, report.macro_f1)


class AbstentionTests(unittest.TestCase):
    def test_abstentions_are_excluded_not_counted_wrong(self):
        truth = ["invoice", "letter", "memo"]
        pred = ["invoice", "unknown", "memo"]

        report = evaluate(truth, pred, LABELS, abstain_label="unknown")

        self.assertEqual(report.abstained, 1)
        self.assertEqual(report.total, 3)
        # 2 scored, both right - abstaining is not the same as being wrong.
        self.assertAlmostEqual(report.accuracy, 1.0)
        self.assertAlmostEqual(report.to_dict()["coverage"], 2 / 3, places=3)

    def test_coverage_is_reported_alongside_accuracy(self):
        report = evaluate(
            ["invoice"] * 4,
            ["unknown"] * 3 + ["invoice"],
            LABELS,
            abstain_label="unknown",
        )
        # Perfect accuracy on 25% coverage must be visibly distinguishable
        # from perfect accuracy on everything.
        self.assertAlmostEqual(report.accuracy, 1.0)
        self.assertAlmostEqual(report.to_dict()["coverage"], 0.25)


class ValidationTests(unittest.TestCase):
    def test_length_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(["invoice"], ["invoice", "letter"], LABELS)

    def test_unknown_truth_label_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(["tax return"], ["invoice"], LABELS)

    def test_unknown_prediction_is_rejected_unless_it_is_the_abstain_label(self):
        with self.assertRaises(ValueError):
            evaluate(["invoice"], ["tax return"], LABELS)


class RenderingTests(unittest.TestCase):
    def test_renderers_produce_text(self):
        report = evaluate(["invoice", "letter"], ["invoice", "memo"], LABELS)
        self.assertIn("macro-F1", render(report))
        self.assertIn("invoice", render_confusion(report))


if __name__ == "__main__":
    unittest.main()
