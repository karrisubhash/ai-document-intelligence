"""What the Search endpoint shows for its hits: highlights that agree with
the search, match-centred snippets, document metadata, grouping by document,
and notices instead of raw diagnostics. Ranking is not under test here -
search.presentation only presents what ranking returned."""

import shutil

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import AuditLog, Collection, Document, ProcessingJob
from documents.processing.chunking import service as chunking_service
from documents.processing.pipeline import run_pipeline
from documents.search import presentation
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user

PROVIDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"


class SpanTests(SimpleTestCase):
    def test_markers_become_offsets_and_words_one_space_apart_join(self):
        original = "The minimum attendance is 75 percent."
        marked = "The \x01minimum\x02 \x01attendance\x02 is 75 percent."

        self.assertEqual(presentation.spans_from_markers(marked, original), [(4, 22)])

    def test_text_that_does_not_round_trip_gets_no_spans(self):
        self.assertEqual(presentation.spans_from_markers("A \x01b\x02 c", "A b  c"), [])

    def test_a_short_text_is_its_own_snippet(self):
        self.assertEqual(
            presentation.snippet_window("Short clause.", [(0, 5)]), (0, 13)
        )

    def test_a_long_text_is_cut_around_the_first_match_at_word_boundaries(self):
        text = (
            ("filler words here " * 40)
            + "the late return fine is five rupees "
            + ("more filler text " * 40)
        )
        anchor = text.index("late")
        start, end = presentation.snippet_window(text, [(anchor, anchor + 4)])

        self.assertLess(start, anchor)
        self.assertGreater(end, anchor + 4)
        self.assertLessEqual(end - start, presentation.SNIPPET_CHARS)
        self.assertEqual(text[start - 1], " ")
        self.assertEqual(text[end], " ")

    def test_file_kinds(self):
        self.assertEqual(presentation.file_kind("a.PDF"), "pdf")
        self.assertEqual(presentation.file_kind("scan.jpeg"), "image")
        self.assertEqual(presentation.file_kind("notes.txt"), "text")
        self.assertEqual(presentation.file_kind("x"), "other")


def result(
    chunk_id, document_id, start, text, *, page=1, relevance=0.9, spans=(), kind=""
):
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "document_title": f"Doc {document_id}",
        "document_type": "POLICY",
        "department": "Physics",
        "document_updated_at": "2026-11-01T00:00:00Z",
        "file_name": "d.pdf",
        "file_kind": "pdf",
        "version_number": 1,
        "page_number": page,
        "character_start": start,
        "character_end": start + len(text),
        "text": text,
        "relevance": relevance,
        "below_floor": False,
        "matched_by": ["lexical"],
        "highlights": [{"start": s, "end": e} for s, e in spans],
        "snippet": {"start": 0, "end": len(text)},
        "chunk_kind": kind,
    }


class GroupingTests(SimpleTestCase):
    PAGE = "Fees are due in July. Late payment costs 50 rupees a week."

    def test_documents_come_in_the_order_of_their_best_passage(self):
        groups = presentation.group_by_document(
            [result(1, 7, 0, "a"), result(2, 8, 0, "b"), result(3, 7, 40, "c", page=2)]
        )

        self.assertEqual([g["document_id"] for g in groups], [7, 8])
        self.assertEqual(len(groups[0]["passages"]), 2)
        self.assertEqual(groups[0]["more_passages"], 0)
        self.assertEqual(groups[0]["document_type"], "POLICY")
        self.assertEqual(groups[0]["file_kind"], "pdf")

    def test_overlapping_chunks_of_one_page_are_one_passage(self):
        first = result(1, 7, 0, self.PAGE[:30], spans=[(0, 4)])
        second = result(2, 7, 20, self.PAGE[20:], relevance=0.95, spans=[(10, 14)])

        [group] = presentation.group_by_document([first, second])

        [passage] = group["passages"]
        self.assertEqual(passage["text"], self.PAGE)
        self.assertEqual(passage["chunk_ids"], [1, 2])
        self.assertEqual(
            (passage["character_start"], passage["character_end"]), (0, len(self.PAGE))
        )
        self.assertEqual(passage["relevance"], 0.95)
        # The second chunk's highlight moved with its text.
        self.assertEqual(
            [self.PAGE[h["start"] : h["end"]] for h in passage["highlights"]],
            ["Fees", self.PAGE[30:34]],
        )

    def test_passages_that_disagree_or_are_tables_stay_apart(self):
        clash = [result(1, 7, 0, "abcdef"), result(2, 7, 3, "XYZghi")]
        table = [result(1, 7, 0, "abcdef"), result(2, 7, 3, "defghi", kind="TABLE")]
        other_page = [result(1, 7, 0, "abcdef"), result(2, 7, 3, "defghi", page=2)]

        for case in (clash, table, other_page):
            self.assertEqual(
                len(presentation.group_by_document(case)[0]["passages"]), 2
            )

    def test_at_most_three_passages_and_a_count_of_the_rest(self):
        hits = [result(n, 7, n * 100, f"passage {n}", page=n) for n in range(1, 6)]

        [group] = presentation.group_by_document(hits)

        self.assertEqual([p["chunk_ids"] for p in group["passages"]], [[1], [2], [3]])
        self.assertEqual(group["more_passages"], 2)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER)
class SearchEndpointPresentationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.user = make_user("presenter", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def index(
        self, title, body, *, department="Registry", name="rules.txt", kind="REGULATION"
    ):
        document = Document.objects.create(
            title=title, document_type=kind, owner=self.user, department=department
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name=name),
            file_name=name,
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        return document

    def search(self, **body):
        response = self.client.post(reverse("search"), body, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_highlights_follow_the_search_s_stemming_not_substrings(self):
        self.index(
            "Contract Rules",
            b"Every contract follows the regulation on fees. The act of signing is binding.",
        )

        data = self.search(query="regulations act")

        hit = data["results"][0]
        marked = [hit["text"][h["start"] : h["end"]] for h in hit["highlights"]]
        self.assertIn("regulation", marked)
        self.assertIn("act", marked)
        # "act" inside "contract" is not a match.
        self.assertNotIn("contract", " ".join(marked))
        for span in hit["highlights"]:
            self.assertGreaterEqual(span["start"], hit["snippet"]["start"])

    def test_each_hit_names_its_document_without_extra_queries(self):
        document = self.index(
            "Fee Rules", b"Fees are due by the end of July.", name="fees.pdf.txt"
        )
        hits = search_service.search(self.user, "fees", limit=5).hits

        with self.assertNumQueries(0):
            payloads = [presentation.hit_payload(hit, []) for hit in hits]
        with self.assertNumQueries(1):
            presentation.highlight_spans(hits, "fees")

        payload = payloads[0]
        self.assertEqual(payload["document_id"], document.pk)
        self.assertEqual(payload["document_type"], "REGULATION")
        self.assertEqual(payload["department"], "Registry")
        self.assertEqual(payload["file_kind"], "text")
        self.assertEqual(payload["version_number"], 1)
        self.assertIsNotNone(payload["document_updated_at"])

    def test_grouped_by_document_on_request(self):
        self.index("Fee Rules", b"Fees are due by the end of July. Late fees apply.")
        self.index("Hostel Rules", b"Hostel fees are paid each term.")

        plain = self.search(query="fees")
        grouped = self.search(query="fees", group_by="document")

        self.assertNotIn("groups", plain)
        titles = {g["document_title"] for g in grouped["groups"]}
        self.assertEqual(titles, {"Fee Rules", "Hostel Rules"})
        group = grouped["groups"][0]
        self.assertEqual(
            set(group),
            {
                "document_id",
                "document_title",
                "document_type",
                "department",
                "updated_at",
                "file_name",
                "file_kind",
                "version_number",
                "relevance",
                "matched_by",
                "passages",
                "more_passages",
            },
        )
        self.assertTrue(group["passages"][0]["highlights"])
        self.assertIn("server_ms", grouped)

    def test_notices_say_what_happened_in_codes(self):
        empty = self.search(query="fees")
        self.assertEqual(
            empty["notices"], [{"code": "NO_ACCESSIBLE_DOCUMENTS", "params": {}}]
        )
        # The diagnostic note is still there, for compatibility.
        self.assertEqual(empty["notes"], ["no authorized documents match the filters"])

        self.index("Fee Rules", b"Fees are due by the end of July.")
        filtered = self.search(query="fees", document_type="SYLLABUS")
        self.assertEqual(
            [n["code"] for n in filtered["notices"]], ["FILTERS_EXCLUDE_ALL"]
        )

        found = self.search(query="fees")
        # The test suite embeds with the hashing baseline.
        self.assertIn("LEXICAL_BASELINE", [n["code"] for n in found["notices"]])

    def test_an_invisible_collection_excludes_everything_but_says_filters(self):
        self.index("Fee Rules", b"Fees are due by the end of July.")
        other = make_user("elsewhere", department="Physics")
        collection = Collection.objects.create(
            name="Theirs", department="Physics", created_by=other
        )

        data = self.search(query="fees", collection_id=collection.pk)

        self.assertEqual(data["results"], [])
        self.assertEqual([n["code"] for n in data["notices"]], ["FILTERS_EXCLUDE_ALL"])

    def test_a_document_with_no_chunks_is_reported_as_not_indexed(self):
        document = Document.objects.create(
            title="Unprocessed", owner=self.user, department="Registry"
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(b"x", name="x.txt"),
            file_name="x.txt",
            file_size=1,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])

        data = self.search(query="anything")

        self.assertEqual(
            data["notices"], [{"code": "NOTHING_INDEXED", "params": {"documents": 1}}]
        )

    def test_the_query_still_never_reaches_the_audit_log(self):
        self.index("Fee Rules", b"Fees are due by the end of July.")

        self.search(query="confidential fees query", group_by="document")

        row = AuditLog.objects.filter(action=AuditLog.Action.SEARCH).latest("id")
        self.assertNotIn("confidential", str(row.metadata))

    def test_group_by_must_be_known(self):
        response = self.client.post(
            reverse("search"), {"query": "fees", "group_by": "page"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
