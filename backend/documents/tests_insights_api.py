"""The document-intelligence API: insights, related, thumbnail, suggestions,
deadlines, and what the document shapes gained.

Authorization first in every test that crosses a department: an invisible
document is a 404 identical to a missing one, and never appears in another
document's related list, the corpus suggestions or the deadlines.
"""

import math
import random
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import UserProfile
from documents.api import insights as briefs
from documents.api.views import DocumentViewSet
from documents.models import (
    AuditLog,
    ClassificationResult,
    Collection,
    Document,
    DocumentCentroid,
    DocumentContent,
    DocumentInsight,
    ExtractionResult,
    ProcessingJob,
)
from documents.processing.chunking import reset_provider_cache
from documents.processing.errors import ErrorCode
from documents.tests import make_user
from documents.tests_ingestion import IngestionTestCase, make_pdf_bytes
from documents.tests_insights import CIRCULAR

SEMANTIC = "documents.tests_type_detection.SemanticHashingProvider"

HOSTEL = """HOSTEL REGULATIONS
Office of the Warden

Clause 3.1 Hostel fee
The hostel fee of Rs. 60,000 per year must be paid before admission to the
hostel. The last date for payment of the hostel fee is 10th July 2099.

Clause 3.2 Visitors
Visitors are permitted in the hostel lounge between 4 pm and 7 pm only.
"""

CONTRACT_KEYS = {
    "document_id",
    "version_id",
    "content_id",
    "status",
    "method",
    "generated_at",
    "summary",
    "key_points",
    "key_phrases",
    "entities",
    "stats",
    "suggested_questions",
    "document_type",
    "ai",
}


class InsightsApiTestCase(IngestionTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.outsider = make_user(
            "physics_faculty", UserProfile.Role.FACULTY, "Physics"
        )

    def upload_text(self, text, title="Doc", name="doc.txt", **fields):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": title,
                    "file": SimpleUploadedFile(
                        name, text.encode(), content_type="text/plain"
                    ),
                    **fields,
                },
                format="multipart",
            )
        self.assertEqual(response.status_code, 202, response.data)
        return response

    def document(self, response):
        return Document.objects.get(pk=response.data["id"])

    def as_outsider(self):
        self.client.force_authenticate(user=self.outsider)


class InsightsEndpointTests(InsightsApiTestCase):
    def test_the_brief_has_the_contract_shape(self):
        document = self.document(self.upload_text(CIRCULAR, title="Transport circular"))
        response = self.client.get(f"/api/v1/documents/{document.pk}/insights/")

        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(set(data), CONTRACT_KEYS)
        self.assertEqual(data["status"], "READY")
        self.assertEqual(data["method"], "extractive-v1")
        self.assertIsNotNone(data["generated_at"])
        self.assertIsNone(data["ai"])
        self.assertEqual(
            data["document_type"],
            {
                "value": "CIRCULAR",
                "source": "auto",
                "confidence": document.type_confidence,
            },
        )
        page = document.current_version.current_content.pages.get(page_number=1).text
        for item in data["summary"]:
            self.assertEqual(set(item), {"text", "page_number", "start", "end"})
            self.assertEqual(page[item["start"] : item["end"]], item["text"])
        for item in data["key_points"]:
            self.assertEqual(set(item), {"text", "page_number", "start", "end", "kind"})
        for item in data["entities"]:
            self.assertEqual(
                set(item),
                {
                    "type",
                    "text",
                    "normalized",
                    "page_number",
                    "start",
                    "end",
                    "count",
                    "label",
                },
            )
            self.assertEqual(page[item["start"] : item["end"]], item["text"])
        deadline = next(e for e in data["entities"] if e["type"] == "DEADLINE")
        self.assertEqual(deadline["label"], "Last date for renewal of the bus pass")
        self.assertEqual(
            set(data["stats"]),
            {"pages", "words", "reading_minutes", "ocr_pages", "language"},
        )
        self.assertTrue(all(isinstance(q, str) for q in data["suggested_questions"]))

    def test_reading_the_brief_writes_no_audit_row(self):
        document = self.document(self.upload_text(CIRCULAR))
        before = AuditLog.objects.count()
        self.client.get(f"/api/v1/documents/{document.pk}/insights/")
        self.assertEqual(AuditLog.objects.count(), before)

    def test_an_outsider_gets_a_404_and_the_attempt_is_audited(self):
        document = self.document(self.upload_text(CIRCULAR))
        self.as_outsider()
        response = self.client.get(f"/api/v1/documents/{document.pk}/insights/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.outsider, document=document, result=AuditLog.Result.DENIED
            ).exists()
        )

    def test_pending_while_the_brief_is_not_yet_written(self):
        document = self.document(self.upload_text(CIRCULAR))
        DocumentInsight.objects.all().delete()
        response = self.client.get(f"/api/v1/documents/{document.pk}/insights/")
        self.assertEqual(response.data["status"], "PENDING")
        self.assertEqual(response.data["summary"], [])
        self.assertEqual(response.data["stats"]["language"], "unknown")

    def test_a_quarantined_version_has_no_brief(self):
        document = self.document(self.upload_text(CIRCULAR))
        version = document.current_version
        ProcessingJob.objects.create(
            document_version=version,
            status=ProcessingJob.Status.FAILED,
            error_code=ErrorCode.MALWARE_DETECTED,
        )
        response = self.client.get(f"/api/v1/documents/{document.pk}/insights/")
        self.assertEqual(response.data["status"], "UNAVAILABLE")
        self.assertIsNone(response.data["content_id"])
        self.assertEqual(response.data["entities"], [])


class RelatedEndpointTests(InsightsApiTestCase):
    def test_lexical_related_documents_and_exact_twins(self):
        first = self.document(self.upload_text(HOSTEL, title="Hostel A"))
        twin = self.document(self.upload_text(HOSTEL, title="Hostel B", name="b.txt"))
        self.upload_text(
            "Minutes of the sports committee meeting on cricket.", title="Sports"
        )

        response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["method"], "lexical")
        results = response.data["results"]
        self.assertEqual([row["id"] for row in results], [twin.pk])
        self.assertTrue(results[0]["near_duplicate"])
        self.assertEqual(
            set(results[0]),
            {
                "id",
                "title",
                "document_type",
                "department",
                "updated_at",
                "similarity",
                "near_duplicate",
            },
        )

    def test_another_departments_document_is_never_related(self):
        first = self.document(self.upload_text(HOSTEL, title="Hostel A"))
        self.as_outsider()
        hidden = self.document(self.upload_text(HOSTEL, title="Physics hostel"))
        self.client.force_authenticate(user=self.user)

        response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertNotIn(hidden.pk, [row["id"] for row in response.data["results"]])

    def test_archived_documents_are_not_related(self):
        first = self.document(self.upload_text(HOSTEL, title="Hostel A"))
        twin = self.document(self.upload_text(HOSTEL, title="Hostel B", name="b.txt"))
        self.client.post(f"/api/v1/documents/{twin.pk}/archive/")
        response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(response.data["results"], [])

    @override_settings(DOCUMENT_EMBEDDING_PROVIDER=SEMANTIC)
    def test_semantic_centroids_rank_neighbours(self):
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)
        first = self.document(self.upload_text(HOSTEL, title="Hostel A"))
        similar = self.document(
            self.upload_text(HOSTEL.replace("Visitors", "Guests"), title="Hostel B")
        )
        response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(response.data["method"], "semantic")
        ids = [row["id"] for row in response.data["results"]]
        self.assertEqual(ids[0], similar.pk)
        self.assertGreater(response.data["results"][0]["similarity"], 0.9)

    def semantic_neighbours_behind_a_crowd(self, crowd=250):
        """A readable neighbour, and `crowd` centroids nearer still that the
        caller may not read.

        The crowd is what a larger institution's index holds in front of a
        small department's documents: other departments' documents, earlier
        passes, other models' vectors. Here they are passes of a Physics
        document, each a hair from `first` - all nearer than the readable
        neighbour, and more of them than the index's 200-candidate probe.
        """
        reset_provider_cache()
        self.addCleanup(reset_provider_cache)
        first = self.document(self.upload_text(HOSTEL, title="Hostel A"))
        similar = self.document(
            self.upload_text(HOSTEL.replace("Visitors", "Guests"), title="Hostel B")
        )
        self.as_outsider()
        hidden = self.document(self.upload_text(CIRCULAR, title="Physics circular"))
        self.client.force_authenticate(user=self.user)

        mine = DocumentCentroid.objects.get(
            document_content=first.current_version.current_content, semantic=True
        )
        version = hidden.current_version
        passes = DocumentContent.objects.bulk_create(
            DocumentContent(document_version=version, processing_version="earlier")
            for _ in range(crowd)
        )
        noise = random.Random(7)
        rows = []
        for content in passes:
            vector = [value + noise.uniform(-1e-3, 1e-3) for value in mine.embedding]
            norm = math.sqrt(sum(value * value for value in vector))
            rows.append(
                DocumentCentroid(
                    document_content=content,
                    document_version=version,
                    model_name=mine.model_name,
                    model_version=mine.model_version,
                    dimensions=len(vector),
                    semantic=True,
                    chunk_count=1,
                    embedding=[value / norm for value in vector],
                )
            )
        DocumentCentroid.objects.bulk_create(rows)

        # Planned as a table of real size is planned: through the
        # nearest-neighbour index. A test table is small enough that sorting
        # every row costs less, and that plan never shows what the index does.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_sort = off")
        return first, similar

    @override_settings(DOCUMENT_EMBEDDING_PROVIDER=SEMANTIC)
    def test_a_readable_neighbour_is_found_behind_nearer_ones_the_caller_cannot_read(
        self,
    ):
        first, similar = self.semantic_neighbours_behind_a_crowd()

        response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(response.data["method"], "semantic")
        self.assertEqual([row["id"] for row in response.data["results"]], [similar.pk])

    @override_settings(DOCUMENT_EMBEDDING_PROVIDER=SEMANTIC)
    def test_past_the_exact_limit_the_index_scan_goes_on_until_it_finds_them(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            installed = tuple(int(part) for part in cursor.fetchone()[0].split(".")[:2])
        if installed < (0, 8):
            self.skipTest("iterative index scans need pgvector 0.8")
        first, similar = self.semantic_neighbours_behind_a_crowd()

        with mock.patch.object(briefs, "RELATED_EXACT_MAX", 0):
            response = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(response.data["method"], "semantic")
        self.assertEqual([row["id"] for row in response.data["results"]], [similar.pk])

    def test_related_is_throttled_as_an_insights_read(self):
        first = self.document(self.upload_text(HOSTEL))
        with mock.patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"insights": "1/minute"}
        ):
            cache.clear()
            ok = self.client.get(f"/api/v1/documents/{first.pk}/related/")
            limited = self.client.get(f"/api/v1/documents/{first.pk}/related/")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(limited.status_code, 429)


class ThumbnailEndpointTests(InsightsApiTestCase):
    def upload_pdf(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": "Scanned",
                    "file": SimpleUploadedFile(
                        "s.pdf",
                        make_pdf_bytes(["Page one"]),
                        content_type="application/pdf",
                    ),
                },
                format="multipart",
            )
        return self.document(response)

    def test_a_pdf_thumbnail_is_served_privately_with_validators(self):
        document = self.upload_pdf()
        before = AuditLog.objects.count()
        response = self.client.get(f"/api/v1/documents/{document.pk}/thumbnail/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(response["Content-Type"], ("image/webp", "image/png"))
        self.assertEqual(response["Cache-Control"], "private, max-age=300")
        self.assertTrue(response["ETag"])
        self.assertTrue(response["Last-Modified"])
        self.assertTrue(b"".join(response.streaming_content))

        again = self.client.get(
            f"/api/v1/documents/{document.pk}/thumbnail/",
            headers={"If-None-Match": response["ETag"]},
        )
        self.assertEqual(again.status_code, 304)
        self.assertEqual(AuditLog.objects.count(), before)

    def test_a_text_document_has_none(self):
        document = self.document(self.upload_text(CIRCULAR))
        response = self.client.get(f"/api/v1/documents/{document.pk}/thumbnail/")
        self.assertEqual(response.status_code, 404)

    def test_an_outsider_gets_a_404(self):
        document = self.upload_pdf()
        self.as_outsider()
        response = self.client.get(f"/api/v1/documents/{document.pk}/thumbnail/")
        self.assertEqual(response.status_code, 404)

    def test_a_quarantined_version_has_none(self):
        document = self.upload_pdf()
        ProcessingJob.objects.create(
            document_version=document.current_version,
            status=ProcessingJob.Status.FAILED,
            error_code=ErrorCode.MALWARE_DETECTED,
        )
        response = self.client.get(f"/api/v1/documents/{document.pk}/thumbnail/")
        self.assertEqual(response.status_code, 404)


class SuggestionsAndDeadlinesTests(InsightsApiTestCase):
    def test_suggestions_come_only_from_readable_documents(self):
        mine = self.document(self.upload_text(HOSTEL, title="Hostel"))
        self.as_outsider()
        self.upload_text(CIRCULAR, title="Physics circular")
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/documents/suggestions/")
        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        self.assertTrue(results)
        self.assertLessEqual(len(results), 8)
        self.assertEqual({row["document_id"] for row in results}, {mine.pk})
        self.assertEqual(set(results[0]), {"question", "document_id", "document_title"})

    def test_cached_suggestions_are_authorised_again(self):
        mine = self.document(self.upload_text(HOSTEL, title="Hostel"))
        self.assertTrue(
            self.client.get("/api/v1/documents/suggestions/").data["results"]
        )
        self.client.post(f"/api/v1/documents/{mine.pk}/archive/")
        self.assertEqual(
            self.client.get("/api/v1/documents/suggestions/").data["results"], []
        )

    def test_upcoming_deadlines_from_readable_documents(self):
        mine = self.document(self.upload_text(HOSTEL, title="Hostel"))
        self.as_outsider()
        self.upload_text(HOSTEL, title="Physics hostel")
        self.client.force_authenticate(user=self.user)

        with mock.patch("documents.api.insights.timezone.localdate") as today:
            import datetime

            today.return_value = datetime.date(2099, 6, 1)
            response = self.client.get("/api/v1/documents/deadlines/?days=90")
            too_far = self.client.get("/api/v1/documents/deadlines/?days=10")

        self.assertEqual(response.status_code, 200)
        results = response.data["results"]
        self.assertEqual(len(results), 1)
        row = results[0]
        self.assertEqual(row["date"], "2099-07-10")
        self.assertEqual(row["type"], "DEADLINE")
        self.assertEqual(row["document_id"], mine.pk)
        self.assertEqual(row["label"], "Last date for payment of the hostel fee")
        self.assertEqual(
            set(row),
            {
                "date",
                "type",
                "label",
                "text",
                "document_id",
                "document_title",
                "page_number",
                "start",
                "end",
            },
        )
        self.assertEqual(too_far.data["results"], [])

    def test_days_is_validated(self):
        for value in ("0", "367", "abc", "-1", "²"):
            response = self.client.get(f"/api/v1/documents/deadlines/?days={value}")
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("days", response.data)


class DocumentShapeTests(InsightsApiTestCase):
    def test_detail_gains_facts_counts_collections_and_uploader(self):
        self.user.first_name, self.user.last_name = "Asha", "Rao"
        self.user.save()
        document = self.document(self.upload_text(CIRCULAR))
        collection = Collection.objects.create(
            name="Transport", department="Computer Science"
        )
        collection.documents.add(document)
        Collection.objects.create(name="Hidden", department="Physics").documents.add(
            document
        )

        data = self.client.get(f"/api/v1/documents/{document.pk}/").data
        self.assertEqual(data["page_count"], 1)
        self.assertFalse(data["has_thumbnail"])
        self.assertIsNone(data["current_stage"])
        self.assertEqual(data["document_type_source"], "auto")
        self.assertIsNotNone(data["type_confidence"])
        self.assertEqual(data["counts"]["tables"], 0)
        self.assertGreater(data["counts"]["fields_found"], 0)
        self.assertEqual(
            data["collections"], [{"id": collection.pk, "name": "Transport"}]
        )
        self.assertEqual(data["current_version"]["uploaded_by_name"], "Asha Rao")

    def test_list_rows_gain_the_listing_facts(self):
        self.upload_text(CIRCULAR)
        row = self.client.get("/api/v1/documents/").data["results"][0]
        for key in (
            "page_count",
            "has_thumbnail",
            "current_stage",
            "document_type_source",
            "type_confidence",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["page_count"], 1)
        self.assertNotIn("counts", row)

    def test_an_in_flight_document_reports_its_stage(self):
        document = self.document(self.upload_text(CIRCULAR))
        Document.objects.filter(pk=document.pk).update(
            status=Document.Status.PROCESSING
        )
        ProcessingJob.objects.create(
            document_version=document.current_version,
            status=ProcessingJob.Status.PROCESSING,
            current_stage=ProcessingJob.Stage.OCR,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/documents/{document.pk}/").data["current_stage"],
            "OCR",
        )
        row = self.client.get("/api/v1/documents/").data["results"][0]
        self.assertEqual(row["current_stage"], "OCR")

    def test_a_changed_type_is_a_persons_choice_and_refreshes_the_fields(self):
        document = self.document(self.upload_text(CIRCULAR))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/documents/{document.pk}/",
                {"document_type": "POLICY"},
                format="multipart",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["document_type_source"], "user")
        self.assertIsNone(response.data["type_confidence"])
        newest = ExtractionResult.objects.filter(
            document_content=document.current_version.current_content
        ).first()
        self.assertEqual(newest.document_type, "POLICY")

    def test_an_unchanged_type_sent_with_a_title_edit_is_not_a_choice(self):
        document = self.document(self.upload_text(CIRCULAR))
        response = self.client.patch(
            f"/api/v1/documents/{document.pk}/",
            {"title": "Renamed", "document_type": "CIRCULAR"},
            format="multipart",
        )
        self.assertEqual(response.data["document_type_source"], "auto")

    def test_a_type_chosen_against_a_type_that_has_since_moved_is_refused(self):
        # The edit form opened on General; the detector has since made the
        # document a circular. The person picked Policy over "General" - a
        # type the document no longer has - so it is not recorded as theirs.
        document = self.document(self.upload_text(CIRCULAR))
        with self.captureOnCommitCallbacks(execute=True) as queued:
            response = self.client.patch(
                f"/api/v1/documents/{document.pk}/",
                {
                    "title": "Renamed",
                    "document_type": "POLICY",
                    "expected_document_type": "GENERAL",
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Circular", response.data["document_type"][0])
        self.assertEqual(queued, [])
        document.refresh_from_db()
        self.assertEqual(document.title, "Doc")
        self.assertEqual(document.document_type, "CIRCULAR")
        self.assertEqual(document.document_type_source, "auto")
        self.assertIsNotNone(document.type_confidence)

    def test_a_type_chosen_against_the_current_type_is_a_persons_choice(self):
        document = self.document(self.upload_text(CIRCULAR))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/documents/{document.pk}/",
                {"document_type": "POLICY", "expected_document_type": "CIRCULAR"},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["document_type"], "POLICY")
        self.assertEqual(response.data["document_type_source"], "user")
        self.assertNotIn("expected_document_type", response.data)

    def test_a_title_edit_never_writes_back_a_type_read_before_the_detector_set_it(
        self,
    ):
        document = self.document(self.upload_text(CIRCULAR))
        # What the request read a moment before the detector's UPDATE
        # committed: the upload default.
        stale = Document.objects.get(pk=document.pk)
        stale.document_type = Document.DocumentType.GENERAL
        stale.document_type_source = Document.TypeSource.DEFAULT
        stale.type_confidence = None
        with mock.patch.object(DocumentViewSet, "get_object", return_value=stale):
            response = self.client.patch(
                f"/api/v1/documents/{document.pk}/",
                {"title": "Renamed"},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.title, "Renamed")
        self.assertEqual(document.document_type, "CIRCULAR")
        self.assertEqual(document.document_type_source, "auto")
        self.assertIsNotNone(document.type_confidence)

    def test_the_source_cannot_be_written_directly(self):
        document = self.document(self.upload_text("Minutes of the meeting."))
        self.client.patch(
            f"/api/v1/documents/{document.pk}/",
            {"document_type_source": "user", "type_confidence": "0.99"},
            format="multipart",
        )
        document.refresh_from_db()
        self.assertEqual(document.document_type_source, "default")
        self.assertIsNone(document.type_confidence)

    def test_an_upload_names_its_readable_exact_duplicates(self):
        first = self.document(self.upload_text(HOSTEL, title="First"))
        self.as_outsider()
        self.upload_text(HOSTEL, title="Physics copy")
        self.client.force_authenticate(user=self.user)

        response = self.upload_text(HOSTEL, title="Again")
        duplicates = response.data["possible_duplicates"]
        self.assertEqual([row["id"] for row in duplicates], [first.pk])
        self.assertEqual(
            set(duplicates[0]),
            {"id", "title", "status", "is_archived", "is_current_version"},
        )
        self.assertTrue(duplicates[0]["is_current_version"])
        unique = self.upload_text("Something nobody uploaded before.", title="New")
        self.assertEqual(unique.data["possible_duplicates"], [])

    def test_classification_lists_the_type_verdict_first_without_server_paths(self):
        document = self.document(self.upload_text(CIRCULAR))
        version = document.current_version
        ClassificationResult.objects.create(
            document_version=version,
            model_name="rvl",
            model_version="1",
            predicted_label="unknown",
            confidence=0.3,
            abstained=True,
            metadata={"artifact": "/models/secret/model.joblib", "top_label": "letter"},
        )
        data = self.client.get(f"/api/v1/documents/{document.pk}/classification/").data
        self.assertEqual(data[0]["taxonomy"], "document_type")
        self.assertEqual(data[1]["taxonomy"], "layout_class")
        self.assertTrue(data[1]["abstained"])
        self.assertNotIn("artifact", data[1]["metadata"])
