"""Search relevance: the passage-level benchmark's scoring definitions.

Hand-worked examples, no database: these functions are what every search
number in docs/evaluation/search-relevance.md is measured against.
"""

from django.test import SimpleTestCase

from documents.evaluation import search as scoring


def case(kind="exact"):
    return {"id": "c", "kind": kind, "split": "test", "query": "q"}


class EvidenceMatchingTests(SimpleTestCase):
    def test_matching_ignores_case_and_line_breaks(self):
        passage = (
            "A student must maintain a minimum of 75 percent\r\nattendance in each"
        )
        self.assertTrue(
            scoring.contains_evidence(passage, "MINIMUM of 75 percent attendance")
        )

    def test_a_partial_phrase_is_not_evidence(self):
        self.assertFalse(
            scoring.contains_evidence("75 percent", "75 percent attendance")
        )


class CaseScoringTests(SimpleTestCase):
    # chunk -> document
    OWNERS = {1: 10, 2: 10, 3: 20, 4: 30, 5: 40, 6: 50}

    def score(self, ranked, evidence, pool=None, k=5):
        return scoring.score_case(
            case(),
            ranked,
            self.OWNERS,
            evidence,
            set(pool if pool is not None else ranked),
            k=k,
        )

    def test_evidence_carried_by_two_overlapping_chunks_counts_once(self):
        """Adjacent chunks overlap, so one clause often sits in both. A page
        showing it once has found all of it."""
        result = self.score([1, 3, 4], [{1, 2}])

        self.assertEqual(result.recall, 1.0)
        self.assertEqual(result.reciprocal_rank, 1.0)

    def test_precision_is_over_what_was_returned(self):
        result = self.score([3, 1], [{1}])

        self.assertEqual(result.precision, 0.5)
        self.assertEqual(result.reciprocal_rank, 0.5)
        self.assertEqual(result.outcome, scoring.TOP_K)

    def test_a_candidate_ordered_out_of_the_top_k_is_a_ranking_failure(self):
        result = self.score([3, 4, 5, 6, 2, 1], [{1}], k=5)

        self.assertEqual(result.recall, 0.0)
        self.assertEqual(result.pool_recall, 1.0)
        self.assertEqual(result.outcome, scoring.RANKING)

    def test_evidence_never_retrieved_is_a_retrieval_failure(self):
        result = self.score([3, 4], [{1}], pool=[3, 4])

        self.assertEqual(result.pool_recall, 0.0)
        self.assertEqual(result.outcome, scoring.RETRIEVAL)

    def test_a_filtered_out_candidate_is_still_a_ranking_failure(self):
        """The pool is what retrieval found; a passage it found that the
        endpoint then dropped was lost to selection, not to retrieval."""
        result = self.score([], [{1}], pool=[1, 3])

        self.assertEqual(result.returned, 0)
        self.assertEqual(result.outcome, scoring.RANKING)

    def test_ndcg_rewards_relevant_passages_higher_up(self):
        first = self.score([1, 3], [{1}])
        second = self.score([3, 1], [{1}])

        self.assertEqual(first.ndcg, 1.0)
        self.assertLess(second.ndcg, first.ndcg)

    def test_document_metrics_collapse_chunks_of_one_document(self):
        result = self.score([3, 4, 1], [{1}])

        # Documents in order: 20, 30, 10 - the relevant one is third.
        self.assertAlmostEqual(result.document_reciprocal_rank, 1 / 3)

    def test_crowding_is_recorded(self):
        result = self.score([1, 2, 3], [{3}])

        self.assertEqual(result.distinct_documents, 2)
        self.assertEqual(result.most_from_one_document, 2)


class NoRelevantCaseTests(SimpleTestCase):
    def test_returning_nothing_is_the_correct_outcome(self):
        result = scoring.score_case(case("no-relevant"), [], {}, [], set(), k=5)

        self.assertFalse(result.answerable)
        self.assertEqual(result.outcome, scoring.CORRECT_EMPTY)

    def test_returning_anything_is_recorded(self):
        result = scoring.score_case(case("no-relevant"), [7], {7: 70}, [], {7}, k=5)

        self.assertEqual(result.outcome, scoring.RESULTS_FOR_NOTHING)


class AggregateTests(SimpleTestCase):
    def test_quality_and_the_cost_of_a_cut_off_are_both_reported(self):
        owners = {1: 10, 2: 20}
        scores = [
            scoring.score_case(case(), [1, 2], owners, [{1}], {1, 2}, k=5),
            # An answerable query that came back empty.
            scoring.score_case(case(), [], owners, [{2}], {2}, k=5),
            scoring.score_case(case("no-relevant"), [], owners, [], set(), k=5),
            scoring.score_case(case("no-relevant"), [2], owners, [], {2}, k=5),
        ]

        report = scoring.aggregate(scores, 5)

        self.assertEqual(report["quality"]["mrr"], 0.5)
        self.assertEqual(report["quality"]["answerable_with_results"], 0.5)
        self.assertEqual(report["quality"]["no_relevant_returned_nothing"], 0.5)
        self.assertEqual(report["diagnostics"]["outcomes"]["ranking"], 1)
