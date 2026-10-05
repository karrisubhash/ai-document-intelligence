"""Phase 8: retrieval, generation and citation metrics; runs and regression.

The metric tests run without a database. They are the definitions every
number this project reports gets measured against, so they are tested
exhaustively by hand-worked examples rather than sampled through the stack.
"""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from documents import rag
from documents.evaluation import loader, metrics, runner
from documents.models import (
    Conversation,
    Document,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    Feedback,
    Message,
    ProcessingJob,
)
from documents.processing.chunking import service as chunking_service
from documents.processing.pipeline import run_pipeline
from documents.tests import MEDIA_ROOT, make_user

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"


# --- Retrieval metrics (pure) ------------------------------------------------


class RetrievalMetricTests(TestCase):
    def test_recall_at_k_counts_relevant_items_found(self):
        self.assertEqual(metrics.recall_at_k([1, 2, 3], [2, 4], 3), 0.5)

    def test_recall_at_k_respects_the_cutoff(self):
        self.assertEqual(metrics.recall_at_k([1, 2, 3], [3], 2), 0.0)
        self.assertEqual(metrics.recall_at_k([1, 2, 3], [3], 3), 1.0)

    def test_recall_with_no_relevant_items_is_zero_not_one(self):
        """Perfect recall over nothing is a vacuous claim, and scoring it
        1.0 would inflate every average it lands in."""
        self.assertEqual(metrics.recall_at_k([1, 2], [], 5), 0.0)

    def test_precision_denominator_is_what_was_returned(self):
        """Charging a system for slots it could not fill measures the
        corpus, not the retriever."""
        self.assertEqual(metrics.precision_at_k([1], [1], 10), 1.0)

    def test_precision_at_k_counts_wrong_answers(self):
        self.assertEqual(metrics.precision_at_k([1, 9, 8, 7], [1], 4), 0.25)

    def test_reciprocal_rank_rewards_an_earlier_hit(self):
        self.assertEqual(metrics.reciprocal_rank([5, 2, 1], [1]), 1 / 3)
        self.assertEqual(metrics.reciprocal_rank([1, 2, 5], [1]), 1.0)

    def test_reciprocal_rank_is_zero_when_nothing_relevant_is_found(self):
        self.assertEqual(metrics.reciprocal_rank([5, 6], [1]), 0.0)

    def test_average_precision_rewards_ranking_all_relevant_items_high(self):
        both_high = metrics.average_precision([1, 2, 9, 8], [1, 2])
        one_low = metrics.average_precision([1, 9, 8, 2], [1, 2])
        self.assertGreater(both_high, one_low)

    def test_ndcg_discounts_by_rank(self):
        top = metrics.ndcg_at_k([1, 9, 8], [1], 3)
        bottom = metrics.ndcg_at_k([9, 8, 1], [1], 3)
        self.assertEqual(top, 1.0)
        self.assertLess(bottom, top)

    def test_aggregate_skips_cases_with_no_relevant_documents(self):
        """Abstention cases have none, and averaging a vacuous 0.0 into
        recall would make a correct refusal look like a miss."""
        report = metrics.aggregate_retrieval([([1], [1]), ([2], [])], ks=(1,))
        self.assertEqual(report.cases, 1)
        self.assertEqual(report.recall_at_k[1], 1.0)


# --- Citation and generation metrics (pure) ---------------------------------


class GenerationMetricTests(TestCase):
    def test_citation_accuracy_catches_citing_the_wrong_document(self):
        self.assertEqual(metrics.citation_accuracy([1, 9], [1]), 0.5)

    def test_citing_nothing_scores_zero_not_one(self):
        """No citations is a failure to ground, not a perfect record of
        never having been wrong."""
        self.assertEqual(metrics.citation_accuracy([], [1]), 0.0)

    def test_source_coverage_measures_what_was_missed(self):
        self.assertEqual(metrics.source_coverage([1], [1, 2]), 0.5)

    def test_token_overlap_is_not_correctness(self):
        """Named for what it measures: a wrong answer built from the right
        vocabulary scores well, which is why it is never reported alone."""
        self.assertEqual(
            metrics.token_overlap(
                "attendance is 75 percent", "attendance is 75 percent"
            ),
            1.0,
        )
        self.assertGreater(
            metrics.token_overlap(
                "attendance is 10 percent", "attendance is 75 percent"
            ),
            0.5,
        )

    def test_abstention_correctness_needs_both_directions(self):
        self.assertTrue(metrics.abstention_correct(True, True))
        self.assertTrue(metrics.abstention_correct(False, False))
        self.assertFalse(metrics.abstention_correct(True, False))

    def test_aggregate_separates_the_two_abstention_failures(self):
        report = metrics.aggregate_generation(
            [
                {"abstained": True, "expected_abstention": False},  # too shy
                {"abstained": False, "expected_abstention": True},  # invented
                {"abstained": True, "expected_abstention": True},  # correct
            ]
        )
        self.assertEqual(report.false_abstentions, 1)
        self.assertEqual(report.missed_abstentions, 1)
        self.assertEqual(report.correct_abstentions, 1)

    def test_answer_quality_is_averaged_over_answered_cases_only(self):
        """Folding abstentions in at 0.0 would make a cautious system look
        inaccurate rather than cautious."""
        report = metrics.aggregate_generation(
            [
                {
                    "abstained": False,
                    "expected_abstention": False,
                    "groundedness": 1.0,
                    "cited_documents": [1],
                    "relevant_documents": [1],
                },
                {"abstained": True, "expected_abstention": True},
            ]
        )
        self.assertEqual(report.groundedness, 1.0)
        self.assertEqual(report.coverage, 0.5)


# --- Regression comparison ---------------------------------------------------


class ComparisonTests(TestCase):
    def test_detects_a_regression(self):
        report = metrics.compare(
            {"retrieval": {"mrr": 0.9}}, {"retrieval": {"mrr": 0.6}}
        )
        self.assertIn("retrieval.mrr", report["regressions"])
        self.assertAlmostEqual(report["deltas"]["retrieval.mrr"], -0.3)

    def test_detects_an_improvement(self):
        report = metrics.compare(
            {"retrieval": {"mrr": 0.6}}, {"retrieval": {"mrr": 0.9}}
        )
        self.assertIn("retrieval.mrr", report["improvements"])

    def test_tolerance_suppresses_noise(self):
        report = metrics.compare({"a": 0.900}, {"a": 0.899}, tolerance=0.01)
        self.assertEqual(report["regressions"], [])

    def test_falling_latency_is_an_improvement_not_a_regression(self):
        """Found by the full suite: the comparison assumed higher is always
        better, so a run that got faster was reported as having regressed."""
        report = metrics.compare(
            {"system": {"latency_ms_p95": 900}},
            {"system": {"latency_ms_p95": 400}},
        )
        self.assertIn("system.latency_ms_p95", report["improvements"])
        self.assertEqual(report["regressions"], [])

    def test_rising_latency_is_a_regression(self):
        report = metrics.compare(
            {"system": {"latency_ms_p95": 400}},
            {"system": {"latency_ms_p95": 900}},
        )
        self.assertIn("system.latency_ms_p95", report["regressions"])

    def test_more_false_abstentions_is_a_regression_not_an_improvement(self):
        """The dangerous half of the same bug: a system that started
        refusing answerable questions would have been reported as improved."""
        report = metrics.compare(
            {"generation": {"false_abstentions": 0}},
            {"generation": {"false_abstentions": 3}},
        )
        self.assertIn("generation.false_abstentions", report["regressions"])

    def test_more_missed_abstentions_is_a_regression(self):
        report = metrics.compare(
            {"generation": {"missed_abstentions": 0}},
            {"generation": {"missed_abstentions": 2}},
        )
        self.assertIn("generation.missed_abstentions", report["regressions"])

    def test_case_counts_are_neither_better_nor_worse(self):
        """A run over more cases is not a quality improvement."""
        report = metrics.compare(
            {"retrieval": {"cases": 10}}, {"retrieval": {"cases": 20}}
        )
        self.assertIn("retrieval.cases", report["changed"])
        self.assertEqual(report["improvements"], [])
        self.assertEqual(report["regressions"], [])

    def test_a_new_metric_is_added_not_a_catastrophic_regression(self):
        """Otherwise introducing a metric reads as a drop from zero."""
        report = metrics.compare({"a": 1.0}, {"a": 1.0, "b": 0.5})
        self.assertEqual(report["added"], ["b"])
        self.assertEqual(report["regressions"], [])


# --- Dataset loading ---------------------------------------------------------


class DatasetLoadingTests(TestCase):
    def payload(self, **overrides):
        data = {
            "name": "institutional-qa",
            "version": "1.0.0",
            "cases": [
                {
                    "question": "What is the minimum attendance?",
                    "expected_answer": "75 percent",
                    "relevant_documents": ["doc_1"],
                    "relevant_pages": [1],
                }
            ],
        }
        data.update(overrides)
        return data

    def test_loads_the_spec_shape(self):
        dataset = loader.load_dataset(self.payload())
        case = dataset.cases.get()

        self.assertEqual(dataset.name, "institutional-qa")
        self.assertEqual(case.relevant_documents, [1])
        self.assertEqual(case.relevant_pages, [1])

    def test_accepts_ids_as_doc_prefixed_strings_or_plain_integers(self):
        dataset = loader.load_dataset(
            self.payload(
                cases=[{"question": "q", "relevant_documents": ["doc_7", 8, "9"]}]
            )
        )
        self.assertEqual(dataset.cases.get().relevant_documents, [7, 8, 9])

    def test_rejects_a_case_with_no_question(self):
        with self.assertRaises(loader.DatasetError):
            loader.load_dataset(self.payload(cases=[{"relevant_documents": ["doc_1"]}]))

    def test_rejects_an_answerable_case_with_no_relevant_documents(self):
        """Retrieval cannot be scored for it, so loading it would silently
        shrink the benchmark."""
        with self.assertRaises(loader.DatasetError):
            loader.load_dataset(self.payload(cases=[{"question": "q"}]))

    def test_rejects_a_case_that_is_both_answerable_and_unanswerable(self):
        with self.assertRaises(loader.DatasetError) as caught:
            loader.load_dataset(
                self.payload(
                    cases=[
                        {
                            "question": "q",
                            "expect_abstention": True,
                            "relevant_documents": ["doc_1"],
                        }
                    ]
                )
            )
        self.assertIn("cannot both", str(caught.exception))

    def test_an_abstention_case_needs_no_relevant_documents(self):
        dataset = loader.load_dataset(
            self.payload(cases=[{"question": "q", "expect_abstention": True}])
        )
        self.assertTrue(dataset.cases.get().expect_abstention)

    def test_datasets_are_immutable_once_loaded(self):
        """Editing questions would make last month's score and this
        month's describe different exams."""
        loader.load_dataset(self.payload())
        with self.assertRaises(loader.DatasetError) as caught:
            loader.load_dataset(self.payload())
        self.assertIn("already exists", str(caught.exception))

    def test_rejects_a_dataset_with_no_name_or_version(self):
        with self.assertRaises(loader.DatasetError):
            loader.load_dataset({"cases": [{"question": "q"}]})


# --- End to end through the live pipeline ------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class EvaluationRunTests(TestCase):
    CORPUS = {
        "Attendance Regulation": (
            b"Clause 4.2 Attendance minimum is 75 percent for examination "
            b"eligibility. Students below it are barred from the examination.\n"
        ),
        "Library Policy": (
            b"Borrowing limit is six books per student for fourteen days.\n"
        ),
    }

    def setUp(self):
        chunking_service.reset_provider_cache()
        rag.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(rag.reset_caches)

        self.user = make_user("eval_user", department="Registry")
        self.documents = {}
        for title, body in self.CORPUS.items():
            self.documents[title] = self.index(title, body)

    def index(self, title, body):
        document = Document.objects.create(
            title=title,
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name="d.txt"),
            file_name="d.txt",
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        return document

    def dataset(self):
        return loader.load_dataset(
            {
                "name": "harness-fixture",
                "version": "1.0.0",
                "department": "Registry",
                "cases": [
                    {
                        "question": "What is the minimum attendance required?",
                        "expected_answer": "75 percent",
                        "relevant_documents": [
                            self.documents["Attendance Regulation"].id
                        ],
                    },
                    {
                        "question": "How many books can a student borrow?",
                        "expected_answer": "six books",
                        "relevant_documents": [self.documents["Library Policy"].id],
                    },
                    {
                        "question": "What is the policy on cryptocurrency mining?",
                        "expect_abstention": True,
                    },
                ],
            }
        )

    def test_a_run_scores_every_case(self):
        run = runner.run_dataset(self.dataset(), self.user)

        self.assertEqual(run.status, EvaluationRun.Status.COMPLETED)
        self.assertEqual(run.cases_scored, 3)
        self.assertEqual(EvaluationCaseResult.objects.filter(run=run).count(), 3)

    def test_retrieval_metrics_are_computed(self):
        run = runner.run_dataset(self.dataset(), self.user)
        retrieval = run.metrics["retrieval"]

        self.assertEqual(retrieval["cases"], 2)
        self.assertGreater(retrieval["recall_at_k"]["1"], 0.0)
        self.assertGreater(retrieval["mrr"], 0.0)

    def test_the_abstention_case_is_scored_as_correct(self):
        run = runner.run_dataset(self.dataset(), self.user)
        generation = run.metrics["generation"]

        self.assertEqual(generation["correct_abstentions"], 1)
        self.assertEqual(generation["missed_abstentions"], 0)

    def test_configuration_is_captured_with_the_metrics(self):
        """Section 19.2: a score detached from what produced it cannot be
        compared to anything."""
        run = runner.run_dataset(self.dataset(), self.user)
        config = run.configuration

        self.assertEqual(config["embedding"]["provider"], "hashing-baseline")
        self.assertFalse(config["embedding"]["semantic"])
        self.assertEqual(config["answerer"]["name"], "extractive-answerer")
        self.assertFalse(config["answerer"]["generative"])
        self.assertIn("chunking", config)

    def test_per_case_results_are_stored_for_tracing(self):
        run = runner.run_dataset(self.dataset(), self.user)
        result = EvaluationCaseResult.objects.filter(
            run=run, case__expect_abstention=False
        ).first()

        self.assertTrue(result.answer)
        self.assertIn("recall_at_k", result.scores)
        self.assertTrue(result.cited_documents)

    def test_a_department_dataset_is_run_only_inside_its_department(self):
        """Stored answers are shown to the dataset's department but produced
        with the run user's access. Run by another department's member, they
        could quote that department's documents; run by a system
        administrator, anyone's."""
        from accounts.models import UserProfile

        outsider = make_user("eval_outsider", department="Mechanical")
        root = make_user("eval_root", UserProfile.Role.SYSTEM_ADMIN, "")
        dataset = self.dataset()
        for user in (outsider, root):
            with self.subTest(user=user.username):
                with self.assertRaises(runner.EvaluationScopeError):
                    runner.run_dataset(dataset, user)
        self.assertFalse(EvaluationRun.objects.exists())

    def test_a_run_that_ran_degraded_says_so(self):
        """Captured from settings before any case ran, the configuration
        named the configured reranker even when every case fell back."""
        from unittest import mock

        real_ask = runner.rag.ask

        def degraded_ask(user, question, **kwargs):
            answer = real_ask(user, question, **kwargs)
            answer.reranker_degraded = True
            return answer

        with mock.patch.object(runner.rag, "ask", side_effect=degraded_ask):
            run = runner.run_dataset(self.dataset(), self.user)

        self.assertEqual(run.metrics["system"]["degraded_reranks"], 3)
        self.assertEqual(run.configuration["degraded"]["reranker"], 3)

    def test_a_healthy_run_records_no_degradation(self):
        run = runner.run_dataset(self.dataset(), self.user)
        self.assertEqual(run.metrics["system"]["degraded_reranks"], 0)
        self.assertNotIn("degraded", run.configuration)

    def test_system_metrics_record_latency(self):
        run = runner.run_dataset(self.dataset(), self.user)
        self.assertIn("latency_ms_p95", run.metrics["system"])

    def test_two_runs_can_be_compared(self):
        dataset = self.dataset()
        baseline = runner.run_dataset(dataset, self.user, label="baseline")
        candidate = runner.run_dataset(dataset, self.user, label="candidate")

        report = runner.compare_runs(baseline, candidate)
        self.assertTrue(report["same_dataset"])
        # Quality metrics must be identical for two runs of the same
        # dataset; latency legitimately jitters and is excluded here.
        quality = [k for k in report["regressions"] if not k.startswith("system.")]
        self.assertEqual(quality, [])

    def test_comparing_different_datasets_is_flagged(self):
        first = runner.run_dataset(self.dataset(), self.user)
        other_dataset = loader.load_dataset(
            {
                "name": "other",
                "version": "1.0.0",
                "department": "Registry",
                "cases": [{"question": "q", "expect_abstention": True}],
            }
        )
        second = runner.run_dataset(other_dataset, self.user)

        report = runner.compare_runs(first, second)
        self.assertFalse(report["same_dataset"])
        self.assertIn("different datasets", report["warning"])

    def test_a_worse_configuration_shows_up_as_a_regression(self):
        """The point of the whole phase: a change that hurts quality must be
        visible as a number, not discovered by a user."""
        dataset = self.dataset()
        baseline = runner.run_dataset(dataset, self.user, label="baseline")

        with override_settings(RAG_RELEVANCE_FLOOR=0.99):
            rag.reset_caches()
            candidate = runner.run_dataset(dataset, self.user, label="too-strict")

        report = runner.compare_runs(baseline, candidate)
        self.assertTrue(report["regressions"])
        self.assertGreater(
            candidate.metrics["generation"]["false_abstentions"],
            baseline.metrics["generation"]["false_abstentions"],
        )


# --- API ---------------------------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class EvaluationAPITests(TestCase):
    def setUp(self):
        chunking_service.reset_provider_cache()
        rag.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(rag.reset_caches)

        self.user = make_user("eval_api_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.dataset = EvaluationDataset.objects.create(
            name="api-fixture", version="1.0.0", department="Registry"
        )
        self.run = EvaluationRun.objects.create(
            dataset=self.dataset,
            status=EvaluationRun.Status.COMPLETED,
            configuration={"answerer": {"name": "extractive-answerer"}},
            metrics={"retrieval": {"mrr": 0.8}, "generation": {"groundedness": 1.0}},
            cases_total=1,
            cases_scored=1,
        )

    def test_requires_authentication(self):
        response = APIClient().get(reverse("evaluation-list"))
        self.assertIn(response.status_code, (401, 403))

    def test_lists_runs_with_their_configuration(self):
        response = self.client.get(reverse("evaluation-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        first = response.data["results"][0]
        self.assertIn("configuration", first)
        self.assertIn("metrics", first)

    def test_a_run_from_another_department_is_hidden(self):
        """A benchmark leaks the corpus it was built from through its
        questions and expected answers."""
        outsider = make_user("eval_api_outsider", department="Mechanical")
        client = APIClient()
        client.force_authenticate(outsider)

        self.assertEqual(client.get(reverse("evaluation-list")).data["count"], 0)
        self.assertEqual(
            client.get(reverse("evaluation-detail", args=[self.run.id])).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_a_user_with_no_department_sees_nothing(self):
        stray = get_user_model().objects.create_user(
            username="eval_stray", password="pass1234"
        )
        client = APIClient()
        client.force_authenticate(stray)
        self.assertEqual(client.get(reverse("evaluation-list")).data["count"], 0)

    def test_detail_returns_one_run(self):
        response = self.client.get(reverse("evaluation-detail", args=[self.run.id]))
        self.assertEqual(response.data["id"], self.run.id)

    def test_compare_endpoint_reports_deltas(self):
        other = EvaluationRun.objects.create(
            dataset=self.dataset,
            status=EvaluationRun.Status.COMPLETED,
            metrics={"retrieval": {"mrr": 0.5}, "generation": {"groundedness": 1.0}},
        )
        response = self.client.get(
            reverse("evaluation-compare", args=[self.run.id, other.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("retrieval.mrr", response.data["regressions"])


# --- Feedback ----------------------------------------------------------------


class FeedbackTests(TestCase):
    def setUp(self):
        self.user = make_user("fb_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        self.conversation = Conversation.objects.create(owner=self.user)
        self.message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content="Attendance minimum is 75 percent. [1]",
            citations=[{"document_id": 1, "marker": 1}],
        )

    def url(self, message=None):
        return reverse("message-feedback", args=[(message or self.message).id])

    def test_records_a_rating(self):
        response = self.client.post(self.url(), {"rating": "HELPFUL"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Feedback.objects.get().rating, "HELPFUL")

    def test_snapshots_what_was_judged(self):
        """A later reprocess changes the answer; feedback about an answer
        that no longer exists must not re-attach to a different one."""
        self.client.post(self.url(), {"rating": "INCORRECT"}, format="json")
        feedback = Feedback.objects.get()

        self.assertEqual(feedback.answer_citations, 1)
        self.assertFalse(feedback.answer_abstained)

    def test_rating_again_updates_rather_than_duplicating(self):
        self.client.post(self.url(), {"rating": "HELPFUL"}, format="json")
        response = self.client.post(self.url(), {"rating": "INCORRECT"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Feedback.objects.count(), 1)
        self.assertEqual(Feedback.objects.get().rating, "INCORRECT")

    def test_cannot_rate_someone_elses_answer(self):
        other = make_user("fb_other", department="Registry")
        client = APIClient()
        client.force_authenticate(other)

        response = client.post(self.url(), {"rating": "HELPFUL"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_cannot_rate_a_user_turn(self):
        question = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.USER,
            content="What is the minimum attendance?",
        )
        response = self.client.post(
            self.url(question), {"rating": "HELPFUL"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rejects_an_unknown_rating(self):
        response = self.client.post(self.url(), {"rating": "MEH"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
