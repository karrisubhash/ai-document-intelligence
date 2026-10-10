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
import shutil
import tempfile
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from documents.models import Document, EvaluationDataset
from documents.tests import MEDIA_ROOT

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"

EVALUATION_DIR = Path(__file__).resolve().parents[2] / "evaluation"
CORPUS_DIR = EVALUATION_DIR / "corpus"
TEMPLATE = EVALUATION_DIR / "benchmark.template.json"


def corpus_slugs(template=TEMPLATE):
    """The documents THIS template is scored against.

    Derived, not listed. A hardcoded set is a second place the corpus is
    defined and it drifts the moment a document is added - which already
    happened once, when v2 added four documents and these assertions started
    reporting 9 != 5.

    Read from the template rather than from the directory, because the
    directory is shared between benchmark versions and the template is what
    governs the exam. Reading the directory was the FIRST fix here and it was
    wrong for the same reason the seeder was: it made v1's corpus depend on
    what v2 happened to need.
    """
    data = json.loads(Path(template).read_text(encoding="utf-8"))
    if data.get("corpus"):
        return {str(slug).lstrip("@") for slug in data["corpus"]}
    return {
        str(reference).lstrip("@")
        for case in data["cases"]
        for reference in (case.get("relevant_documents") or [])
        if str(reference).startswith("@")
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
        self.assertEqual(
            {document.title.removeprefix("[eval] ") for document in documents},
            corpus_slugs(),
        )
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
            len(corpus_slugs()),
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

    # --- The corpus is part of the dataset -------------------------------

    def test_only_the_templates_own_documents_are_ingested(self):
        """The corpus directory holds every version's documents; a benchmark
        is scored against the ones ITS template names.

        This is the assertion that would have caught the v2 regression: four
        documents landed in `evaluation/corpus/` for a different benchmark,
        the seeder ingested the directory, and v1's committed baseline
        stopped matching a system that had not changed.
        """
        self.seed()

        ingested = {
            document.title.removeprefix("[eval] ")
            for document in Document.objects.filter(title__startswith="[eval] ")
        }
        on_disk = {source.stem for source in CORPUS_DIR.glob("*.txt")}

        self.assertEqual(ingested, corpus_slugs())
        self.assertLess(
            ingested,
            on_disk | ingested,
            "this test is vacuous unless the directory holds more than v1",
        )

    def test_a_second_benchmark_under_the_same_user_is_refused(self):
        """Retrieval sees everything the evaluator can read, so two corpora
        under one user is a third, unnamed exam. It must fail loudly - the
        scores it produces look like an ordinary quality regression."""
        self.seed()

        other = self.write_template(
            "institutional-other", "v9", ["@grievance-procedure"]
        )
        with self.assertRaises(CommandError) as caught:
            call_command("seed_evaluation", template=str(other), verbosity=0)

        message = str(caught.exception)
        self.assertIn("--user", message)
        self.assertIn("--prune", message)

    def test_prune_switches_a_database_between_versions(self):
        self.seed()

        other = self.write_template(
            "institutional-other", "v9", ["@grievance-procedure"]
        )
        with tempfile.TemporaryDirectory() as directory:
            call_command(
                "seed_evaluation",
                template=str(other),
                output=str(Path(directory) / "b.json"),
                prune=True,
                verbosity=0,
            )

        self.assertEqual(
            {
                document.title.removeprefix("[eval] ")
                for document in Document.objects.filter(title__startswith="[eval] ")
            },
            {"grievance-procedure"},
        )

    def test_an_explicit_corpus_can_include_a_document_no_case_cites(self):
        """A distractor is legitimate - it is what makes a retrieval score
        mean anything - so a template may name its corpus outright."""
        template = self.write_template(
            "institutional-other",
            "v9",
            ["@grievance-procedure"],
            corpus=["grievance-procedure", "library-rules"],
        )
        with tempfile.TemporaryDirectory() as directory:
            call_command(
                "seed_evaluation",
                template=str(template),
                output=str(Path(directory) / "b.json"),
                verbosity=0,
            )

        self.assertEqual(
            {
                document.title.removeprefix("[eval] ")
                for document in Document.objects.filter(title__startswith="[eval] ")
            },
            {"grievance-procedure", "library-rules"},
        )

    def test_a_missing_corpus_file_fails_loudly(self):
        """Resolving without it would score retrieval as a miss for a
        document the benchmark expected to be there."""
        template = self.write_template("institutional-other", "v9", ["@not-a-document"])

        with self.assertRaises(CommandError) as caught:
            call_command("seed_evaluation", template=str(template), verbosity=0)

        self.assertIn("not-a-document.txt", str(caught.exception))

    def write_template(self, name, version, references, corpus=None):
        """A minimal template on disk, for the multi-version cases."""
        payload = {
            "name": name,
            "version": version,
            "description": "fixture",
            "cases": [
                {
                    "question": "What does it say?",
                    "expected_answer": "something",
                    "relevant_documents": references,
                    "relevant_pages": [1],
                }
            ],
        }
        if corpus is not None:
            payload["corpus"] = corpus

        directory = tempfile.mkdtemp(prefix="template-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "template.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


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

        options = {
            "dataset": "institutional-seed",
            "dataset_version": "v1",
            "as_user": "ci-evaluator",
            "stdout": StringIO(),
        }
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
            self.run_evaluation(baseline=str(self.baseline), fail_on_regression=True)

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

    def earlier_run(self):
        """A finished run of the same dataset, for --compare-to."""
        from documents.models import EvaluationRun

        self.run_evaluation(label="earlier")
        return EvaluationRun.objects.get(label="earlier")

    def test_compare_to_leaves_latency_out_unless_asked(self):
        """--compare-to compared every timing regardless of --gate-latency,
        so an unchanged system failed on wall-clock jitter - the failure
        latency was taken out of the gate for."""
        earlier = self.earlier_run()
        # A latency no run can match.
        earlier.metrics["system"]["latency_ms_mean"] = -1
        earlier.save(update_fields=["metrics"])

        self.run_evaluation(compare_to=earlier.id, fail_on_regression=True)

        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(
                compare_to=earlier.id, fail_on_regression=True, gate_latency=True
            )
        self.assertIn("regressed", str(caught.exception))

    def test_compare_to_honours_the_tolerance(self):
        """--tolerance was ignored by --compare-to."""
        earlier = self.earlier_run()
        # A quality drift inside the tolerance.
        earlier.metrics["generation"]["groundedness"] += 0.001
        earlier.save(update_fields=["metrics"])

        self.run_evaluation(
            compare_to=earlier.id, fail_on_regression=True, tolerance=0.01
        )

        with self.assertRaises(CommandError):
            self.run_evaluation(compare_to=earlier.id, fail_on_regression=True)

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
