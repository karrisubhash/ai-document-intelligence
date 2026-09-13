"""Phase 5: chunking, embeddings, indexing and hybrid retrieval.

The splitter tests avoid the database entirely - it is a pure function, and
keeping its tests pure is what proves a chunking strategy can be benchmarked
offline before it is adopted.
"""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from documents.models import (
    AuditLog,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    ProcessingJob,
)
from documents.processing.chunking import service as chunking_service
from documents.processing.chunking import splitter
from documents.processing.chunking.exceptions import EmbeddingProviderNotConfigured
from documents.processing.embedding.hashing import HashingEmbeddingProvider, embed_text
from documents.processing.pipeline import run_pipeline
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user

PROVIDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"


# --- Splitter (pure, no database) -------------------------------------------


class SplitterTests(TestCase):
    def test_short_text_is_one_chunk(self):
        chunks = splitter.split_page(1, "A short clause.")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "A short clause.")

    def test_blank_page_yields_nothing(self):
        """An empty chunk is not a retrievable unit."""
        self.assertEqual(splitter.split_page(1, "   \n\n  "), [])

    def test_offsets_reslice_out_of_the_page(self):
        text = "First paragraph here.\n\n" + ("Second paragraph. " * 80)
        chunks = splitter.split_page(1, text, chunk_size=200, overlap=40)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(
                text[chunk.character_start : chunk.character_end], chunk.text
            )

    def test_verify_accepts_consistent_chunks(self):
        text = "Clause one. " * 100
        chunks = splitter.split_page(1, text, chunk_size=200, overlap=30)
        splitter.verify(chunks, [(1, text)])

    def test_verify_rejects_a_chunk_whose_offsets_lie(self):
        text = "Clause one. " * 20
        chunks = splitter.split_page(1, text, chunk_size=100, overlap=10)
        bad = splitter.Chunk(
            page_number=1,
            chunk_index=0,
            text="something else entirely",
            character_start=0,
            character_end=10,
        )
        with self.assertRaises(ValueError):
            splitter.verify([bad, *chunks], [(1, text)])

    def test_verify_rejects_a_chunk_citing_a_missing_page(self):
        bad = splitter.Chunk(
            page_number=9, chunk_index=0, text="x", character_start=0, character_end=1
        )
        with self.assertRaises(ValueError):
            splitter.verify([bad], [(1, "x")])

    def test_chunks_never_span_pages(self):
        """Page number is half of a citation; a straddling chunk has none."""
        pages = [(1, "Page one content. " * 40), (2, "Page two content. " * 40)]
        chunks = splitter.split_pages(pages, chunk_size=200, overlap=20)

        for chunk in chunks:
            source = dict(pages)[chunk.page_number]
            self.assertIn(chunk.text[:20], source)

    def test_chunk_index_is_continuous_across_pages(self):
        pages = [(1, "alpha " * 60), (2, "beta " * 60)]
        chunks = splitter.split_pages(pages, chunk_size=150, overlap=20)
        self.assertEqual(
            [chunk.chunk_index for chunk in chunks], list(range(len(chunks)))
        )

    def test_overlap_keeps_neighbouring_chunks_connected(self):
        text = "sentence number {}. ".format
        body = "".join(text(i) for i in range(60))
        chunks = splitter.split_page(1, body, chunk_size=300, overlap=100)

        self.assertGreater(len(chunks), 2)
        for earlier, later in zip(chunks, chunks[1:]):
            self.assertLess(later.character_start, earlier.character_end)

    def test_rejects_nonsense_parameters(self):
        with self.assertRaises(ValueError):
            splitter.split_page(1, "x", chunk_size=0)
        with self.assertRaises(ValueError):
            splitter.split_page(1, "x", chunk_size=100, overlap=100)


# --- Embedding baseline ------------------------------------------------------


class HashingEmbeddingTests(TestCase):
    def test_is_deterministic_across_calls(self):
        """The worker that indexes and the process that searches must agree."""
        self.assertEqual(embed_text("attendance policy"), embed_text("attendance policy"))

    def test_vector_has_the_declared_width(self):
        provider = HashingEmbeddingProvider()
        batch = provider.embed(["hello world"])
        self.assertEqual(len(batch.vectors[0]), provider.dimensions)

    def test_vectors_are_unit_length(self):
        vector = embed_text("some ordinary sentence")
        norm = sum(value * value for value in vector) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_empty_text_is_all_zeros_not_a_nonsense_direction(self):
        self.assertEqual(set(embed_text("   ")), {0.0})

    def test_shared_vocabulary_scores_higher_than_unrelated_text(self):
        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b))

        query = embed_text("attendance requirement for examinations")
        related = embed_text("attendance minimum is 75 percent for examination")
        unrelated = embed_text("borrowing limit is six books per student")

        self.assertGreater(cosine(query, related), cosine(query, unrelated))

    def test_reports_itself_as_not_semantic(self):
        """Overstating what this embedder does would misrepresent results."""
        provider = HashingEmbeddingProvider()
        self.assertFalse(provider.semantic)
        self.assertFalse(provider.embed(["x"]).semantic)


# --- Indexing through the pipeline -------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class IndexingTests(TestCase):
    BODY = (
        b"ACADEMIC REGULATIONS R20\n"
        b"Clause 4.2 Attendance minimum is 75 percent for examination eligibility.\n"
        b"Students below this threshold are barred from the end-semester examination.\n"
    )

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.user = make_user("search_user", department="Registry")
        self.document = Document.objects.create(
            title="Attendance Regulation",
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        self.version = self.document.versions.create(
            version_number=1,
            file=ContentFile(self.BODY, name="reg.txt"),
            file_name="reg.txt",
            file_size=len(self.BODY),
            uploaded_by=self.user,
        )
        self.document.current_version = self.version
        self.document.save(update_fields=["current_version"])
        self.job = ProcessingJob.objects.create(document_version=self.version)

    def test_chunks_are_written_with_provenance(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=PROVIDER):
            run_pipeline(self.job.id)

        chunk = DocumentChunk.objects.get()
        self.assertEqual(chunk.page_number, 1)
        self.assertEqual(chunk.chunking_strategy, chunking_service.CHUNKING_STRATEGY)

        page = self.version.contents.first().pages.get(page_number=1)
        self.assertEqual(
            page.text[chunk.character_start : chunk.character_end], chunk.text
        )

    def test_embeddings_are_written_and_labelled(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=PROVIDER):
            run_pipeline(self.job.id)

        embedding = DocumentChunkEmbedding.objects.get()
        self.assertEqual(embedding.model_name, "hashing-baseline")
        self.assertEqual(embedding.dimensions, 384)
        self.assertFalse(embedding.semantic)

    def test_chunks_are_still_written_without_an_embedding_provider(self):
        """Half a search beats none, and vectors can be backfilled."""
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=""):
            run_pipeline(self.job.id)

        self.assertTrue(DocumentChunk.objects.exists())
        self.assertFalse(DocumentChunkEmbedding.objects.exists())

    def test_search_vector_is_populated_for_lexical_search(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=""):
            run_pipeline(self.job.id)
        self.assertIsNotNone(DocumentChunk.objects.first().search_vector)

    def test_reprocessing_replaces_chunks_rather_than_duplicating_them(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=PROVIDER):
            run_pipeline(self.job.id)
            first = DocumentChunk.objects.count()
            run_pipeline(self.job.id)

        self.assertEqual(DocumentChunk.objects.count(), first)
        self.assertEqual(DocumentChunkEmbedding.objects.count(), first)

    def test_the_indexing_stage_reports_what_it_did(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=PROVIDER):
            run_pipeline(self.job.id)

        event = self.job.events.get(stage=ProcessingJob.Stage.INDEXING)
        self.assertEqual(event.status, "SUCCEEDED")
        self.assertGreater(event.metadata["chunks"], 0)
        self.assertFalse(event.metadata["semantic"])


# --- Hybrid retrieval --------------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER)
class HybridSearchTests(TestCase):
    CORPUS = {
        "Attendance Regulation": (
            b"Clause 4.2 Attendance minimum is 75 percent for examination "
            b"eligibility. Students below it are barred from the examination.\n"
        ),
        "Revaluation Circular": (
            b"Circular No: REG/2024/117\nRevaluation requests close 15 days "
            b"after result publication. A fee of 500 rupees applies.\n"
        ),
        "Library Policy": (
            b"Borrowing limit is six books per student for fourteen days. "
            b"Overdue fines accrue at two rupees per day.\n"
        ),
    }

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.user = make_user("hybrid_user", department="Registry")
        for title, body in self.CORPUS.items():
            self.index(title, body, self.user, "Registry")

    def index(self, title, body, owner, department):
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

    def titles(self, response):
        return [hit.citation["document_title"] for hit in response.hits]

    def test_exact_identifier_is_found_by_the_lexical_half(self):
        """Section 12.2: lexical search is what finds codes and numbers."""
        response = search_service.search(self.user, "REG/2024/117", limit=3)

        self.assertEqual(self.titles(response)[0], "Revaluation Circular")
        self.assertGreater(response.lexical_candidates, 0)

    def test_natural_language_query_finds_the_right_document(self):
        response = search_service.search(
            self.user, "attendance percentage required for exams", limit=3
        )
        self.assertEqual(self.titles(response)[0], "Attendance Regulation")

    def test_a_chunk_matched_by_both_halves_outranks_one_matched_by_either(self):
        response = search_service.search(self.user, "REG/2024/117", limit=3)
        top = response.hits[0]

        self.assertEqual(set(top.matched_by), {"lexical", "vector"})
        if len(response.hits) > 1:
            self.assertGreater(top.score, response.hits[1].score)

    def test_results_carry_a_citation_shape(self):
        response = search_service.search(self.user, "borrowing limit", limit=1)
        citation = response.hits[0].citation

        for key in (
            "document_id",
            "page_number",
            "chunk_id",
            "character_start",
            "character_end",
            "text",
        ):
            self.assertIn(key, citation)

    def test_empty_query_returns_nothing_rather_than_everything(self):
        response = search_service.search(self.user, "   ", limit=5)
        self.assertEqual(response.hits, [])

    def test_document_type_filter_narrows_results(self):
        response = search_service.search(
            self.user, "attendance", limit=5, document_type="CIRCULAR"
        )
        self.assertEqual(response.hits, [])

    def test_response_says_the_embeddings_are_not_semantic(self):
        response = search_service.search(self.user, "attendance", limit=1)
        self.assertFalse(response.semantic)
        self.assertTrue(any("not semantic" in note for note in response.notes))

    def test_without_a_provider_search_still_works_lexically(self):
        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=""):
            chunking_service.reset_provider_cache()
            response = search_service.search(self.user, "borrowing limit", limit=3)

        self.assertGreater(len(response.hits), 0)
        self.assertEqual(response.vector_candidates, 0)
        self.assertTrue(any("lexical results only" in n for n in response.notes))


# --- Authorization: the security property ------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER)
class SearchAuthorizationTests(TestCase):
    """Section 14: similarity is never an authorization mechanism."""

    SECRET = b"Confidential mechanical department budget allocation for 2024.\n"

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.mech = make_user("mech_owner", department="Mechanical")
        self.cs = make_user("cs_outsider", department="Computer Science")

        document = Document.objects.create(
            title="Mechanical Budget",
            document_type="POLICY",
            owner=self.mech,
            department="Mechanical",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(self.SECRET, name="budget.txt"),
            file_name="budget.txt",
            file_size=len(self.SECRET),
            uploaded_by=self.mech,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        self.document = document

    def test_owner_can_find_their_own_document(self):
        response = search_service.search(self.mech, "budget allocation", limit=5)
        self.assertGreater(len(response.hits), 0)

    def test_other_department_finds_nothing(self):
        response = search_service.search(self.cs, "budget allocation", limit=5)
        self.assertEqual(response.hits, [])

    def test_other_department_cannot_even_see_a_candidate_count(self):
        """Counts and scores leak existence, so filtering must precede
        ranking rather than follow it."""
        response = search_service.search(self.cs, "budget allocation", limit=5)
        self.assertEqual(response.lexical_candidates, 0)
        self.assertEqual(response.vector_candidates, 0)

    def test_a_user_with_no_department_finds_nothing(self):
        stray = get_user_model().objects.create_user(
            username="search_stray", password="pass1234"
        )
        response = search_service.search(stray, "budget allocation", limit=5)
        self.assertEqual(response.hits, [])

    def test_system_admin_can_search_across_departments(self):
        root = make_user("search_root", role="SYSTEM_ADMIN", department="Registry")
        response = search_service.search(root, "budget allocation", limit=5)
        self.assertGreater(len(response.hits), 0)

    def test_search_scope_matches_the_document_list_scope(self):
        """Search must never surface what the list endpoint would refuse."""
        from documents import authorization

        visible = set(
            authorization.visible_documents(
                self.cs, Document.objects.all()
            ).values_list("id", flat=True)
        )
        response = search_service.search(self.cs, "budget", limit=50)
        found = {hit.citation["document_id"] for hit in response.hits}

        self.assertTrue(found.issubset(visible))


# --- The API surface ---------------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=PROVIDER)
class SearchAPITests(TestCase):
    BODY = b"Revaluation requests close 15 days after result publication.\n"

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.user = make_user("api_search_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        document = Document.objects.create(
            title="Revaluation Circular",
            document_type="CIRCULAR",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(self.BODY, name="c.txt"),
            file_name="c.txt",
            file_size=len(self.BODY),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def search(self, **payload):
        return self.client.post(reverse("search"), payload, format="json")

    def test_requires_authentication(self):
        client = APIClient()
        response = client.post(reverse("search"), {"query": "x"}, format="json")
        self.assertIn(response.status_code, (401, 403))

    def test_returns_results_with_citations(self):
        response = self.search(query="revaluation requests")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(response.data["count"], 0)
        first = response.data["results"][0]
        self.assertEqual(first["document_title"], "Revaluation Circular")
        self.assertIn("character_start", first)

    def test_rejects_a_missing_query(self):
        self.assertEqual(self.search().status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_an_over_long_query(self):
        response = self.search(query="x" * 5000)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_limit_is_capped(self):
        response = self.search(query="revaluation", limit=500)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reports_whether_results_are_semantic(self):
        response = self.search(query="revaluation")
        self.assertFalse(response.data["semantic"])
        self.assertEqual(response.data["embedding_model"], "hashing-baseline 0.1.0")

    def test_search_is_audited_without_recording_the_query(self):
        """A search should be on record without the audit log becoming a
        second copy of what people looked for."""
        self.search(query="confidential salary information")

        entry = AuditLog.objects.filter(action=AuditLog.Action.SEARCH).latest(
            "created_at"
        )
        self.assertEqual(entry.user, self.user)
        self.assertNotIn("confidential", str(entry.metadata).lower())
        self.assertIn("hits", entry.metadata)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class StaleContentRetrievalTests(TestCase):
    """Retrieval answers from the CURRENT version's CURRENT extraction pass.

    Both halves were missing, and the first one produced the worst failure
    this system is capable of: not a missing answer, but a confidently
    cited wrong one.
    """

    R20 = (
        "ACADEMIC REGULATIONS R20\n"
        "Clause 4.2 Attendance minimum is 75 percent for examination eligibility.\n"
    )
    R24 = (
        "ACADEMIC REGULATIONS R24\n"
        "Clause 4.2 Attendance minimum is 60 percent for examination eligibility.\n"
    )

    def setUp(self):
        self.user = make_user("stale_user", department="Registry")
        self.document = Document.objects.create(
            title="Regulations",
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )

    def add_version(self, number, body, name):
        version = self.document.versions.create(
            version_number=number,
            file=ContentFile(body.encode(), name=name),
            file_name=name,
            file_size=len(body),
            uploaded_by=self.user,
        )
        self.document.current_version = version
        self.document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        return version

    def test_a_superseded_version_is_not_retrieved(self):
        """Measured before the fix: the answerer replied "...is 75 percent
        [1] ... is 60 percent [2]" - quoting the WITHDRAWN regulation
        beside the current one, in identical citation formatting, with no
        way for a reader to tell which rule is in force."""
        old = self.add_version(1, self.R20, "r20.txt")
        self.add_version(2, self.R24, "r24.txt")

        chunks, _ = search_service.authorized_chunks(self.user)
        versions = {chunk.document_version_id for chunk in chunks}

        self.assertNotIn(old.id, versions)
        texts = " ".join(chunk.text for chunk in chunks)
        self.assertIn("60 percent", texts)
        self.assertNotIn("75 percent", texts)

    def test_the_superseded_answer_never_reaches_the_reader(self):
        from documents import rag

        self.add_version(1, self.R20, "r20.txt")
        self.add_version(2, self.R24, "r24.txt")

        answer = rag.ask(self.user, "What is the attendance minimum?")

        self.assertFalse(answer.abstained)
        self.assertIn("60 percent", answer.answer)
        self.assertNotIn("75 percent", answer.answer)

    def test_history_is_retained_even_though_it_is_not_retrieved(self):
        """Not retrieving is not the same as deleting. The version endpoints
        still serve the old content; it is simply not what a question gets
        answered from."""
        old = self.add_version(1, self.R20, "r20.txt")
        self.add_version(2, self.R24, "r24.txt")

        self.assertTrue(
            DocumentChunk.objects.filter(document_version=old).exists(),
            "superseded chunks were deleted rather than merely excluded",
        )

    def test_reprocessing_does_not_duplicate_a_passage_in_retrieval(self):
        """A reprocess creates a new DocumentContent beside the old one on
        purpose, so two pipeline versions can be compared - but each pass
        writes a full set of chunks. Measured before the fix: one reprocess
        took search from 2 hits to 3, of which 2 were unique."""
        version = self.add_version(1, self.R20, "r20.txt")
        before, _ = search_service.authorized_chunks(self.user)
        before_count = before.count()

        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

        self.assertEqual(version.contents.count(), 2, "the fixture no longer reprocesses")

        after, _ = search_service.authorized_chunks(self.user)
        texts = [chunk.text for chunk in after]

        self.assertEqual(after.count(), before_count)
        self.assertEqual(len(texts), len(set(texts)), "a passage was retrievable twice")

    def test_the_live_pass_is_the_newest_one(self):
        version = self.add_version(1, self.R20, "r20.txt")
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

        version.refresh_from_db()
        newest = version.contents.order_by("-created_at", "-id").first()

        self.assertEqual(version.current_content_id, newest.id)

    def test_a_document_with_no_current_version_retrieves_nothing(self):
        """Defensive: a version pointer that is NULL must exclude, never
        fall through to everything."""
        self.add_version(1, self.R20, "r20.txt")
        self.document.current_version = None
        self.document.save(update_fields=["current_version"])

        chunks, _ = search_service.authorized_chunks(self.user)

        self.assertEqual(chunks.count(), 0)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class EmptySearchDiagnosticTests(TestCase):
    """When search returns nothing, say WHICH nothing.

    The note used to read "no authorized documents match the filters"
    whenever the chunk queryset was empty. Observed during a live test:
    four documents listed and processed, search returning nothing, and a
    note insisting there were no authorized documents - pointing away from
    the actual cause, which was that every chunk belonged to an extraction
    pass that was no longer the live one.
    """

    def setUp(self):
        self.user = make_user("diagnostic_user", department="Registry")

    def make_document(self, title="Regulations"):
        document = Document.objects.create(
            title=title,
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        body = b"Clause 4.2 Attendance minimum is 75 percent.\n"
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name="r.txt"),
            file_name="r.txt",
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        return document, version

    def test_no_documents_says_so(self):
        response = search_service.search(self.user, "attendance")

        self.assertEqual(response.hits, [])
        self.assertIn("no authorized documents", response.notes[0])

    def test_documents_that_were_never_indexed_are_named_as_such(self):
        """Distinct from a permission problem: the documents are visible,
        they simply have no chunks."""
        self.make_document()

        response = search_service.search(self.user, "attendance")

        self.assertEqual(response.hits, [])
        note = " ".join(response.notes)
        self.assertIn("has been indexed", note)
        self.assertIn("1 authorized document", note)

    def test_chunks_from_a_superseded_pass_are_named_as_such(self):
        """The case that actually happened. The fix must point AT the
        cause, not away from it."""
        document, version = self.make_document()
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        self.assertTrue(
            DocumentChunk.objects.filter(document_version=version).exists()
        )

        # Exactly what a stale worker leaves behind: content and chunks
        # exist, but nothing is marked as the live pass.
        version.current_content = None
        version.save(update_fields=["current_content"])

        response = search_service.search(self.user, "attendance")

        self.assertEqual(response.hits, [])
        note = " ".join(response.notes)
        self.assertIn("current extraction pass", note)
        self.assertIn("worker", note)
        self.assertNotIn("no authorized documents", note)

    def test_a_healthy_document_still_returns_hits(self):
        """The guard must not fire on the ordinary path."""
        document, version = self.make_document()
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

        response = search_service.search(self.user, "attendance")

        self.assertTrue(response.hits)
