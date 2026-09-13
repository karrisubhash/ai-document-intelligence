"""The regression gate can actually fail (§19.2).

Two separate reasons it could not, before this:

  * `evaluation/benchmark.json` did not exist, so the CI step skipped;
  * even with a dataset, `--fail-on-regression` only does anything
    alongside `--compare-to <run id>`, and a fresh CI database has no
    previous run - so the gate ran the benchmark and passed regardless of
    the scores.

A gate that cannot fail is worse than no gate: it occupies the slot, and
everyone downstream believes quality is being checked.
"""

import json
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from documents.models import Document, EvaluationDataset
from documents.tests import MEDIA_ROOT, make_user

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"
CORPUS_SLUGS = {
    "academic-regulations",
    "examination-policy",
    "fee-structure",
    "hostel-regulations",
    "library-rules",
}


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class SeedEvaluationTests(TestCase):
    def seed(self, output=None):
        from io import StringIO

        out = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(output or Path(directory) / "benchmark.json")
            call_command("seed_evaluation", output=str(target), stdout=out)
            data = json.loads(target.read_text(encoding="utf-8"))
        return data, out.getvalue()

    def test_the_corpus_is_ingested_through_the_real_pipeline(self):
        """Inserting rows directly would score retrieval against a fixture
        rather than against extraction, chunking and indexing as
        configured."""
        self.seed()

        documents = Document.objects.filter(title__startswith="[eval] ")
        self.assertEqual(documents.count(), len(CORPUS_SLUGS))
        for document in documents:
            self.assertIsNotNone(document.current_version)
            self.assertTrue(
                document.current_version.contents.exists(),
                f"{document.title} produced no content",
            )

    def test_slugs_are_resolved_to_real_document_ids(self):
        data, _ = self.seed()

        for case in data["cases"]:
            for reference in case.get("relevant_documents", []):
                self.assertIsInstance(reference, int, reference)
                self.assertTrue(Document.objects.filter(id=reference).exists())

    def test_document_ids_are_stable_across_reseeds(self):
        """A dataset is immutable once loaded and stores document ids. A
        seeder that recreated the corpus would leave every previously
        loaded benchmark pointing at rows that no longer exist, and score
        retrieval as a total miss against documents sitting right there."""
        first, _ = self.seed()
        second, _ = self.seed()

        def ids(data):
            return [
                reference
                for case in data["cases"]
                for reference in case.get("relevant_documents", [])
            ]

        self.assertEqual(ids(first), ids(second))

    def test_reseeding_does_not_accumulate_documents(self):
        self.seed()
        self.seed()

        self.assertEqual(
            Document.objects.filter(title__startswith="[eval] ").count(),
            len(CORPUS_SLUGS),
        )

    def test_the_evaluator_cannot_log_in(self):
        """The account exists to own documents and to be the subject of a
        permission check, never to authenticate."""
        self.seed()

        from django.contrib.auth import get_user_model

        user = get_user_model().objects.get(username="ci-evaluator")
        self.assertFalse(user.has_usable_password())

    def test_every_case_is_answerable_or_expects_an_abstention(self):
        data, _ = self.seed()

        for case in data["cases"]:
            has_documents = bool(case.get("relevant_documents"))
            expects_abstention = bool(case.get("expect_abstention"))
            self.assertNotEqual(
                has_documents,
                expects_abstention,
                f"{case['question']!r} is both or neither",
            )

    def test_the_summary_reports_both_kinds_of_case(self):
        _, output = self.seed()

        self.assertIn("answerable", output)
        self.assertIn("expecting abstention", output)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class BaselineGateTests(TestCase):
    """--baseline is what makes the gate work in CI, where --compare-to
    cannot: there is no previous run to name."""

    def setUp(self):
        from documents.processing.chunking import service as chunking_service

        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.benchmark = Path(self.directory.name) / "benchmark.json"
        self.baseline = Path(self.directory.name) / "baseline.json"

        call_command("seed_evaluation", output=str(self.benchmark), verbosity=0)
        call_command("evaluate", load=str(self.benchmark), verbosity=0)

    def run_evaluation(self, **kwargs):
        from io import StringIO

        options = dict(
            dataset="institutional-seed",
            dataset_version="v1",
            as_user="ci-evaluator",
            stdout=StringIO(),
        )
        options.update(kwargs)
        call_command("evaluate", **options)
        return options["stdout"].getvalue()

    def test_a_baseline_records_the_configuration_beside_the_scores(self):
        """A score is only comparable against one produced the same way - a
        baseline set with a semantic embedder is not a baseline for a
        lexical one."""
        self.run_evaluation(write_baseline=str(self.baseline))

        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        self.assertEqual(payload["dataset"], "institutional-seed")
        self.assertIn("configuration", payload)
        self.assertIn("retrieval", payload["metrics"])

    def test_an_unchanged_system_passes(self):
        self.run_evaluation(write_baseline=str(self.baseline))

        self.run_evaluation(baseline=str(self.baseline), fail_on_regression=True)

    def test_a_quality_regression_fails(self):
        """The assertion that makes this a gate rather than a report."""
        self.run_evaluation(write_baseline=str(self.baseline))

        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        payload["metrics"]["generation"]["groundedness"] = 0.999
        self.baseline.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(
                baseline=str(self.baseline), fail_on_regression=True
            )

        self.assertIn("groundedness", str(caught.exception))

    def test_latency_does_not_gate_by_default(self):
        """Wall-clock timings differ on every run and between machines.
        Gating on them makes the check fail permanently while saying
        nothing about quality - and a gate that always fails gets
        disabled, taking the real check with it."""
        self.run_evaluation(write_baseline=str(self.baseline))

        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        payload["metrics"]["system"]["latency_ms_mean"] = 0.001
        payload["metrics"]["system"]["latency_ms_p95"] = 0.001
        self.baseline.write_text(json.dumps(payload), encoding="utf-8")

        # Passes, because latency is excluded...
        self.run_evaluation(baseline=str(self.baseline), fail_on_regression=True)

        # ...and is still checkable when asked for.
        with self.assertRaises(CommandError):
            self.run_evaluation(
                baseline=str(self.baseline),
                fail_on_regression=True,
                gate_latency=True,
            )

    def test_a_baseline_for_another_dataset_is_refused(self):
        """Comparing scores from different benchmarks is comparing
        different exams."""
        self.run_evaluation(write_baseline=str(self.baseline))

        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        payload["dataset_version"] = "v99"
        self.baseline.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(baseline=str(self.baseline))

        self.assertIn("different exams", str(caught.exception))

    def test_a_missing_baseline_is_an_error_not_a_skip(self):
        """The original failure mode: no file, so the gate quietly passed."""
        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(baseline=str(self.baseline / "nope.json"))

        self.assertIn("No baseline", str(caught.exception))

    def test_the_dataset_is_immutable_once_loaded(self):
        with self.assertRaises(Exception):
            call_command("evaluate", load=str(self.benchmark), verbosity=0)

        self.assertEqual(
            EvaluationDataset.objects.filter(name="institutional-seed").count(), 1
        )
