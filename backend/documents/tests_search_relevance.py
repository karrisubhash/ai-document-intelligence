"""Search relevance: the Search endpoint's ranking stage, and the scoring
definitions of the passage-level benchmark that measures it.

The scoring tests are hand-worked examples with no database: those functions
are what every number in docs/evaluation/search-relevance.md is measured
against. The ranking tests use a stand-in cross-encoder that scores passages
by marker phrases, so they pin the endpoint's behaviour without loading a
model - the model's own quality is what the benchmark measures.
"""

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from documents.evaluation import search as scoring
from documents.models import Document, ProcessingJob
from documents.processing.chunking import service as chunking_service
from documents.processing.pipeline import run_pipeline
from documents.rag import engine
from documents.rag.reranking import RerankedChunk
from documents.search import ranking
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user

PROVIDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"
FAKE = "documents.tests_search_relevance.FakeCrossEncoder"
FAKE_MINILM = "documents.tests_search_relevance.FakeMiniLM"


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
            list(pool if pool is not None else ranked),
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

    def test_pool_rank_is_the_first_relevant_position_in_retrieval_order(self):
        """How deep a reranker must look: measured in retrieval's order,
        not in whatever order the endpoint returned."""
        result = self.score([1], [{1}], pool=[3, 4, 1, 2])

        self.assertEqual(result.pool_rank, 3)
        self.assertIsNone(self.score([3], [{1}], pool=[3]).pool_rank)

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

    def test_returning_only_passages_marked_below_the_floor_says_no_match(self):
        result = scoring.score_case(
            case("no-relevant"),
            [7, 8],
            {7: 70, 8: 80},
            [],
            [7, 8],
            k=5,
            below_floor={7, 8},
        )

        self.assertEqual(result.matched, 0)
        self.assertEqual(result.outcome, scoring.SAID_NO_MATCH)

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
            # Returned the closest passage, marked below the floor.
            scoring.score_case(
                case("no-relevant"), [1], owners, [], {1}, k=5, below_floor={1}
            ),
        ]

        report = scoring.aggregate(scores, 5)

        self.assertEqual(report["quality"]["mrr"], 0.5)
        self.assertEqual(report["quality"]["answerable_with_results"], 0.5)
        self.assertEqual(report["quality"]["answerable_with_a_match"], 0.5)
        self.assertAlmostEqual(
            report["quality"]["no_relevant_returned_nothing"], 1 / 3, 4
        )
        self.assertAlmostEqual(report["quality"]["no_relevant_said_no_match"], 2 / 3, 4)
        self.assertEqual(report["diagnostics"]["outcomes"]["ranking"], 1)


# --- The ranking stage --------------------------------------------------------


class FakeCrossEncoder:
    """A cross-encoder stand-in: a passage scores what its marker phrase says.

    Records every candidate list it is shown, so a test can assert what the
    reranker was - and was not - allowed to see.
    """

    name = "fake-cross-encoder"
    version = "fake-model"
    semantic = True
    semantic_scores = True

    scores = {}
    degraded = False
    seen = []

    def rerank(self, query, candidates):
        FakeCrossEncoder.seen.append([c.chunk.id for c in candidates])
        reranked = []
        for candidate in candidates:
            score = max(
                (
                    s
                    for phrase, s in self.scores.items()
                    if phrase in candidate.chunk.text
                ),
                default=0.0,
            )
            signals = {"reranker_degraded": "unavailable"} if self.degraded else {}
            reranked.append(
                RerankedChunk(
                    chunk=candidate.chunk,
                    score=score,
                    retrieval_score=candidate.score,
                    signals=signals,
                )
            )
        reranked.sort(key=lambda r: (-r.score, -r.retrieval_score, r.chunk.id))
        return reranked


class FakeMiniLM(FakeCrossEncoder):
    """Reports the pilot's model id, so ranking applies the floor calibrated
    for it rather than a fake model's."""

    version = "cross-encoder/ms-marco-MiniLM-L6-v2"


def index(title, body, owner, department):
    document = Document.objects.create(
        title=title, document_type="REGULATION", owner=owner, department=department
    )
    version = document.versions.create(
        version_number=1,
        file=ContentFile(body, name="d.txt"),
        file_name="d.txt",
        file_size=len(body),
        uploaded_by=owner,
    )
    document.current_version = version
    document.save(update_fields=["current_version"])
    run_pipeline(ProcessingJob.objects.create(document_version=version).id)
    return document


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER=PROVIDER,
    RAG_RERANKER=FAKE,
    SEARCH_RELEVANCE_FLOOR=None,
    SEARCH_RERANK_DEPTH=30,
)
class RankingTests(TestCase):
    # Each body carries a marker phrase the stand-in cross-encoder scores.
    LIBRARY = b"LIBRARY RULES Clause 9.4 Overdue. The late return fine is 5 rupees per day per book.\n"
    FRAGMENT = b"EXAM1NATION CEL| Fina| Resu|ts Dead|ine: I 2 late fine return\n"
    HOSTEL = (
        b"HOSTEL REGULATIONS Clause 3.1 Curfew. The curfew is 11 pm on Saturdays.\n"
    )

    def setUp(self):
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(engine.reset_caches)

        FakeCrossEncoder.scores = {"late return fine is": 0.97, "Curfew": 0.40}
        FakeCrossEncoder.degraded = False
        FakeCrossEncoder.seen = []
        patcher = mock.patch.dict(ranking.CALIBRATED_FLOORS, {"fake-model": 0.5})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = make_user("ranking_user", department="Registry")
        self.library = index("Library Rules", self.LIBRARY, self.user, "Registry")
        self.fragment = index("Scanned fragment", self.FRAGMENT, self.user, "Registry")
        self.hostel = index("Hostel Regulations", self.HOSTEL, self.user, "Registry")

        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def search(self, **payload):
        response = self.client.post(reverse("search"), payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def titles(self, data):
        return [hit["document_title"] for hit in data["results"]]

    def test_the_answer_outranks_a_fragment_that_matched_words_or_vectors(self):
        """The reproduced failure: an OCR fragment that one retriever ranked
        first sat above the clause that answers the query."""
        data = self.search(query="library late return fine")

        self.assertEqual(self.titles(data), ["Library Rules"])
        self.assertEqual(data["results"][0]["relevance"], 0.97)
        self.assertEqual(data["ranking"]["reranker"], "fake-cross-encoder fake-model")
        self.assertEqual(data["ranking"]["relevance_floor"], 0.5)
        self.assertEqual(data["ranking"]["below_floor"], 2)

    @override_settings(SEARCH_FALLBACK_RESULTS=2)
    def test_a_query_nothing_answers_says_so_and_marks_the_closest(self):
        """The other reproduced failure: a full page of unrelated passages,
        presented as results, for a query no document is about. Now: an
        explicit no-match, with only the closest few, each marked."""
        FakeCrossEncoder.scores = {"Curfew": 0.40}

        data = self.search(query="quantum chromodynamics lattice gauge theory")

        self.assertEqual(data["count"], 2)
        self.assertTrue(all(hit["below_floor"] for hit in data["results"]))
        # Still in relevance order: the 0.40 passage first.
        self.assertEqual(data["results"][0]["document_title"], "Hostel Regulations")
        self.assertEqual(data["ranking"]["below_floor"], 3)
        self.assertTrue(any("close enough match" in note for note in data["notes"]))

    @override_settings(SEARCH_FALLBACK_RESULTS=0)
    def test_without_a_fallback_a_query_nothing_answers_gets_nothing(self):
        FakeCrossEncoder.scores = {}

        data = self.search(query="quantum chromodynamics lattice gauge theory")

        self.assertEqual(data["results"], [])
        self.assertTrue(any("none is shown" in note for note in data["notes"]))

    def test_asking_for_weaker_matches_still_says_none_is_a_close_match(self):
        FakeCrossEncoder.scores = {}

        data = self.search(query="quantum chromodynamics", include_below_floor=True)

        self.assertEqual(data["count"], 3)
        self.assertTrue(all(hit["below_floor"] for hit in data["results"]))
        self.assertTrue(any("close enough match" in note for note in data["notes"]))

    def test_the_fallback_never_applies_when_something_cleared_the_floor(self):
        data = self.search(query="library late return fine")

        self.assertEqual([hit["below_floor"] for hit in data["results"]], [False])

    def test_passages_below_the_floor_are_one_request_away_and_marked(self):
        data = self.search(query="late fine", include_below_floor=True)

        self.assertEqual(self.titles(data)[0], "Library Rules")
        self.assertEqual(
            [hit["below_floor"] for hit in data["results"]], [False, True, True]
        )
        # Below the floor, still in relevance order: the hostel clause the
        # stand-in scores 0.40 before the fragment it scores 0.
        self.assertEqual(
            self.titles(data)[1:], ["Hostel Regulations", "Scanned fragment"]
        )

    def test_every_weaker_match_is_returned_even_when_matches_fill_the_limit(self):
        """The page's "Show N weaker matches" asks again with the same limit
        and include_below_floor; N is ranking.below_floor. With matches and
        weaker matches cut to `limit` together, `limit` matches clearing the
        floor meant the click returned the same matches again - and nothing
        it promised."""
        for number in range(4):
            index(f"Library copy {number}", self.LIBRARY, self.user, "Registry")
        # Five passages score 0.97, above the 0.5 floor; the hostel clause
        # (0.40) and the fragment (0) fall below it.
        first = self.search(query="library late return fine", limit=2)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["ranking"]["below_floor"], 2)

        data = self.search(
            query="library late return fine", limit=2, include_below_floor=True
        )

        self.assertEqual(
            [hit["below_floor"] for hit in data["results"]], [False, False, True, True]
        )
        self.assertEqual(
            self.titles(data)[2:], ["Hostel Regulations", "Scanned fragment"]
        )

    def test_with_nothing_above_the_floor_all_weaker_matches_are_returned(self):
        """ "Show all N weaker matches" must show N, not `limit` of them."""
        FakeCrossEncoder.scores = {}

        data = self.search(query="late fine", limit=2, include_below_floor=True)

        self.assertEqual(data["ranking"]["below_floor"], 3)
        self.assertEqual(data["count"], 3)
        self.assertTrue(all(hit["below_floor"] for hit in data["results"]))

    def test_citations_are_the_stored_passage_exactly(self):
        hit = self.search(query="library late return fine")["results"][0]
        chunk = self.library.current_version.chunks.get()

        self.assertEqual(hit["chunk_id"], chunk.id)
        self.assertEqual(hit["document_id"], self.library.id)
        self.assertEqual(hit["page_number"], chunk.page_number)
        self.assertEqual(
            (hit["character_start"], hit["character_end"]),
            (chunk.character_start, chunk.character_end),
        )
        self.assertEqual(hit["text"], chunk.text)

    def test_an_uncalibrated_cross_encoder_reorders_but_hides_nothing(self):
        """A floor belongs to one model's scores; none is guessed for a
        model nobody measured."""
        ranking.CALIBRATED_FLOORS.pop("fake-model")

        data = self.search(query="late fine")

        self.assertEqual(self.titles(data)[0], "Library Rules")
        self.assertEqual(data["count"], 3)
        self.assertIsNone(data["ranking"]["relevance_floor"])
        self.assertEqual(data["ranking"]["below_floor"], 0)

    def test_an_explicit_floor_overrides_the_calibrated_one(self):
        with override_settings(SEARCH_RELEVANCE_FLOOR=0.3):
            data = self.search(query="late fine")

        self.assertEqual(self.titles(data), ["Library Rules", "Hostel Regulations"])

    def test_a_degraded_cross_encoder_keeps_the_fused_order_unfiltered(self):
        """Its fallback scores are lexical, and lexical reordering measured
        worse than the fused order - so that order is kept, and said."""
        FakeCrossEncoder.degraded = True
        fused = search_service.search(self.user, "late fine", limit=10)

        data = self.search(query="late fine")

        self.assertEqual(
            [hit["chunk_id"] for hit in data["results"]],
            [hit.chunk.id for hit in fused.hits],
        )
        self.assertTrue(data["ranking"]["degraded"])
        self.assertTrue(any("retrieval order" in note for note in data["notes"]))

    def test_the_lexical_reranker_keeps_the_fused_order(self):
        with override_settings(RAG_RERANKER=""):
            engine.reset_caches()
            fused = search_service.search(self.user, "late fine", limit=10)
            data = self.search(query="late fine")

        self.assertEqual(
            [hit["chunk_id"] for hit in data["results"]],
            [hit.chunk.id for hit in fused.hits],
        )
        self.assertEqual(data["ranking"]["reranker"], "")
        self.assertEqual(FakeCrossEncoder.seen, [])

    def test_depth_zero_turns_reranking_off(self):
        with override_settings(SEARCH_RERANK_DEPTH=0):
            data = self.search(query="late fine")

        self.assertEqual(data["count"], 3)
        self.assertEqual(FakeCrossEncoder.seen, [])

    def test_the_reranker_reads_at_least_as_many_candidates_as_were_asked_for(self):
        with override_settings(SEARCH_RERANK_DEPTH=1):
            self.search(query="late fine", limit=3, include_below_floor=True)

        self.assertEqual(len(FakeCrossEncoder.seen[0]), 3)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER=PROVIDER,
    RAG_RERANKER=FAKE,
    SEARCH_RELEVANCE_FLOOR=0.5,
)
class RankingAuthorizationTests(TestCase):
    """Section 14 again, for the new stage: the reranker reorders what the
    permission filter let through and is never shown anything else - not
    even a passage it would have scored highest of all."""

    SECRET = b"Mechanical budget: late return fine is waived for staff.\n"
    VISIBLE = b"Library: the late return fine is 5 rupees per day.\n"

    def setUp(self):
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(engine.reset_caches)
        FakeCrossEncoder.scores = {"late return fine is": 0.9, "waived": 1.0}
        FakeCrossEncoder.degraded = False
        FakeCrossEncoder.seen = []

        self.mech = make_user("rank_mech", department="Mechanical")
        self.cs = make_user("rank_cs", department="Computer Science")
        self.secret = index("Mechanical Budget", self.SECRET, self.mech, "Mechanical")
        self.visible = index("Library", self.VISIBLE, self.cs, "Computer Science")

        self.client = APIClient()

    def search(self, user, **payload):
        self.client.force_authenticate(user)
        return self.client.post(reverse("search"), payload, format="json").data

    def test_another_departments_passage_is_never_shown_to_the_reranker(self):
        data = self.search(self.cs, query="late return fine", include_below_floor=True)

        returned = {hit["document_id"] for hit in data["results"]}
        shown = {chunk for call in FakeCrossEncoder.seen for chunk in call}
        secret_chunks = set(
            self.secret.current_version.chunks.values_list("id", flat=True)
        )

        self.assertEqual(returned, {self.visible.id})
        self.assertFalse(shown & secret_chunks)
        self.assertEqual(data["retrieval"]["authorized_documents"], 1)

    def test_a_department_filter_narrows_before_reranking(self):
        admin = make_user("rank_admin", role="SYSTEM_ADMIN", department="Registry")

        data = self.search(
            admin, query="late return fine", department="Computer Science"
        )

        self.assertEqual(
            {hit["document_id"] for hit in data["results"]}, {self.visible.id}
        )
        shown = {chunk for call in FakeCrossEncoder.seen for chunk in call}
        self.assertFalse(
            shown & set(self.secret.current_version.chunks.values_list("id", flat=True))
        )

    def test_an_administrator_sees_both_ordered_by_relevance(self):
        admin = make_user("rank_admin2", role="SYSTEM_ADMIN", department="Registry")

        data = self.search(admin, query="late return fine")

        self.assertEqual(
            [hit["document_id"] for hit in data["results"]],
            [self.secret.id, self.visible.id],
        )


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER=PROVIDER,
    RAG_RERANKER=FAKE_MINILM,
    SEARCH_RERANK_DEPTH=30,
)
class PilotDefaultsTests(TestCase):
    """The pilot's relevance floor and fallback, at their shipped defaults.

    Nothing here is patched or overridden. CI's search gate runs the lexical
    reranker, which keeps retrieval order and never reaches the floor, and
    the other ranking tests patch a fake model's floor or override the
    fallback - so without these, MiniLM's floor (0.03, chosen on the dev
    split) or the fallback (3) could change and every gate would pass.
    """

    def setUp(self):
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(engine.reset_caches)
        FakeCrossEncoder.scores = {}
        FakeCrossEncoder.degraded = False
        FakeCrossEncoder.seen = []

        self.user = make_user("pilot_user", department="Registry")
        for title, marker in (
            ("Clause A", "alpha"),
            ("Clause B", "bravo"),
            ("Clause C", "charlie"),
            ("Clause D", "delta"),
            ("Clause E", "echo"),
        ):
            body = f"Rule {marker}: late fines are charged per day.\n".encode()
            index(title, body, self.user, "Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def search(self, **payload):
        response = self.client.post(reverse("search"), payload, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def use_code_defaults(self):
        """The defaults in the code, whatever this environment configured."""
        del settings.SEARCH_RELEVANCE_FLOOR
        del settings.SEARCH_FALLBACK_RESULTS

    @override_settings()
    def test_the_minilm_floor_admits_0_05_and_not_0_02(self):
        self.use_code_defaults()
        FakeCrossEncoder.scores = {"alpha": 0.05, "bravo": 0.02}

        data = self.search(query="late fines", include_below_floor=True)

        below = {hit["document_title"]: hit["below_floor"] for hit in data["results"]}
        self.assertFalse(below["Clause A"])  # fails if the floor rises above 0.05
        self.assertTrue(below["Clause B"])  # fails if it falls to 0.02
        self.assertEqual(data["ranking"]["relevance_floor"], 0.03)

    @override_settings()
    def test_with_nothing_above_the_floor_the_three_closest_are_shown(self):
        self.use_code_defaults()
        FakeCrossEncoder.scores = {"alpha": 0.02, "bravo": 0.01}

        data = self.search(query="late fines")

        self.assertEqual(data["count"], 3)
        self.assertTrue(all(hit["below_floor"] for hit in data["results"]))
        self.assertEqual(
            [hit["document_title"] for hit in data["results"]][:2],
            ["Clause A", "Clause B"],
        )

    @unittest.skipIf(
        {"SEARCH_RELEVANCE_FLOOR", "SEARCH_FALLBACK_RESULTS"} & set(os.environ),
        "this environment overrides the shipped defaults",
    )
    def test_the_settings_ship_the_same_defaults(self):
        self.assertIsNone(settings.SEARCH_RELEVANCE_FLOOR)
        self.assertEqual(settings.SEARCH_FALLBACK_RESULTS, 3)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER, RAG_RERANKER=""
)
class SearchBurstThrottleTests(TestCase):
    """A reranked search costs seconds of CPU. The hourly budget alone let
    one account spend all 120 of its searches in the hour's first seconds."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.user = make_user("burst_user", department="Registry")
        index("Library Rules", RankingTests.LIBRARY, self.user, "Registry")

    def search(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(reverse("search"), {"query": "late fine"}, format="json")

    def test_the_twenty_first_search_in_a_minute_is_refused(self):
        codes = [self.search(self.user).status_code for _ in range(21)]

        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)

    def test_the_per_minute_budget_is_per_account(self):
        other = make_user("burst_other", department="Registry")
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES,
            {"search": "1000/hour", "search_burst": "2/minute"},
        ):
            codes = [self.search(self.user).status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            self.assertEqual(self.search(other).status_code, 200)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    DOCUMENT_EMBEDDING_PROVIDER=PROVIDER,
    RAG_RERANKER="",
)
class SearchLimitTests(TestCase):
    def setUp(self):
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.user = make_user("limit_user", department="Registry")
        for number in range(6):
            index(
                f"Notice {number}",
                b"Notice about the late return fine.\n",
                self.user,
                "Registry",
            )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, **payload):
        return self.client.post(reverse("search"), payload, format="json")

    @override_settings(SEARCH_DEFAULT_LIMIT=3, SEARCH_MAX_LIMIT=5)
    def test_the_default_and_the_ceiling_are_configuration(self):
        response = self.post(query="late return fine")

        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["limit"], 3)
        self.assertEqual(response.data["max_limit"], 5)
        self.assertEqual(self.post(query="late fine", limit=5).data["count"], 5)
        self.assertEqual(self.post(query="late fine", limit=6).status_code, 400)

    def test_the_defaults_are_unchanged(self):
        response = self.post(query="late return fine")

        self.assertEqual(response.data["limit"], 10)
        self.assertEqual(response.data["max_limit"], 50)
        self.assertEqual(self.post(query="late", limit=51).status_code, 400)


# --- The benchmark command ----------------------------------------------------


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER, RAG_RERANKER=""
)
class EvaluateSearchCommandTests(TestCase):
    """The committed benchmark runs end to end - seeded through the real
    pipeline, scored through the real view - and gates on a baseline."""

    TEMPLATE = (
        Path(__file__).resolve().parents[2]
        / "evaluation"
        / "search-relevance.template.json"
    )

    @classmethod
    def setUpTestData(cls):
        chunking_service.reset_provider_cache()
        engine.reset_caches()
        call_command(
            "seed_evaluation",
            template=str(cls.TEMPLATE),
            user="search-evaluator",
            output=str(Path(tempfile.mkdtemp()) / "resolved.json"),
            stdout=StringIO(),
        )
        chunking_service.reset_provider_cache()

    def run_command(self, *args):
        out = StringIO()
        call_command(
            "evaluate_search", "--as-user", "search-evaluator", *args, stdout=out
        )
        return out.getvalue()

    def test_every_label_resolves_and_every_case_is_scored(self):
        report_path = Path(tempfile.mkdtemp()) / "report.json"

        self.run_command("--output", str(report_path))
        report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["metrics"]["all"]["diagnostics"]["cases"], 55)
        self.assertEqual(report["metrics"]["test"]["diagnostics"]["answerable"], 30)
        # Every answerable case's evidence was a retrieval candidate: a broken
        # label raises instead of scoring as a miss.
        self.assertEqual(report["metrics"]["all"]["quality"]["pool_recall"], 1.0)

    def test_a_regression_against_the_baseline_fails_the_command(self):
        report_path = Path(tempfile.mkdtemp()) / "report.json"
        self.run_command("--output", str(report_path))

        inflated = json.loads(report_path.read_text(encoding="utf-8"))
        inflated["metrics"]["all"]["quality"]["mrr"] = 1.01
        report_path.write_text(json.dumps(inflated), encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_command("--baseline", str(report_path), "--fail-on-regression")

    def dev_baseline(self):
        path = Path(tempfile.mkdtemp()) / "baseline.json"
        self.run_command("--split", "dev", "--output", str(path))
        return path

    def test_a_run_whose_reranker_degraded_is_refused(self):
        """A cross-encoder that cannot load (empty model cache offline, OOM)
        degrades every search to retrieval order. Scored, that order would be
        published under the cross-encoder's name."""
        FakeCrossEncoder.degraded = True
        self.addCleanup(setattr, FakeCrossEncoder, "degraded", False)
        self.addCleanup(engine.reset_caches)

        with override_settings(RAG_RERANKER=FAKE):
            engine.reset_caches()
            with self.assertRaisesMessage(CommandError, "reranker was unavailable"):
                self.run_command("--split", "dev")

    def test_a_baseline_from_a_different_exam_is_refused(self):
        """--k 10 against a k=5 baseline silently left the k-dependent
        metrics out of the gate; --split test against a dev baseline reported
        the different cases as regressions. Neither is a comparison."""
        baseline = self.dev_baseline()
        runs = {
            "k 5 there": ["--split", "dev", "--k", "10"],
            "limit 10 there": ["--split", "dev", "--limit", "5"],
            "split 'dev' there": ["--split", "test"],
        }
        for message, args in runs.items():
            with self.subTest(message), self.assertRaisesMessage(CommandError, message):
                self.run_command(*args, "--baseline", str(baseline))

    def test_a_gated_metric_missing_from_the_run_fails_the_gate(self):
        """Otherwise renaming or dropping a metric takes it out of the gate
        without anyone deciding to."""
        baseline = self.dev_baseline()
        data = json.loads(baseline.read_text(encoding="utf-8"))
        data["metrics"]["all"]["quality"]["retired_metric"] = 0.5
        baseline.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "all.retired_metric"):
            self.run_command(
                "--split", "dev", "--baseline", str(baseline), "--fail-on-regression"
            )

    def test_refreshing_a_baseline_in_place_compares_with_the_old_one(self):
        """--output X --baseline X is the natural way to refresh a baseline.
        Writing first compared the run with itself, which cannot fail."""
        baseline = self.dev_baseline()
        data = json.loads(baseline.read_text(encoding="utf-8"))
        data["metrics"]["all"]["quality"]["mrr"] = 1.01
        baseline.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_command(
                "--split",
                "dev",
                "--output",
                str(baseline),
                "--baseline",
                str(baseline),
                "--fail-on-regression",
            )

        # ...and the baseline it failed against was not overwritten.
        kept = json.loads(baseline.read_text(encoding="utf-8"))
        self.assertEqual(kept["metrics"]["all"]["quality"]["mrr"], 1.01)
