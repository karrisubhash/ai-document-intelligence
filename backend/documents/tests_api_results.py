"""The read-only endpoints that expose what the pipeline produced.

`/content/` already published the extracted text; these three publish what
the AI stages made of it - the classification verdict, the extracted
fields, and the chunks search runs over. They exist so the pipeline's
output is inspectable through the API rather than only through the admin
or a psql session.

Each one is a read of the document's substance, so each is covered by the
same access policy and the same audit trail as retrieving the document.
"""

import shutil

from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from accounts.models import UserProfile
from documents.models import (
    AuditLog,
    ClassificationResult,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    ExtractedField,
    ExtractionResult,
)
from documents.tests import (
    MEDIA_ROOT,
    AuthenticatedAPITestCase,
    make_txt_file,
    make_user,
)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ResultEndpointTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        self.document = Document.objects.get(id=response.data["id"])
        self.version = self.document.current_version
        self.content = self.version.contents.first()

    # --- classification --------------------------------------------------

    def test_classification_is_an_empty_list_when_the_stage_skipped(self):
        """Not a 404: the endpoint answered, and the answer is "none".

        A 404 would say the document has no classification *resource*,
        which is a different claim from "no classifier was configured, so
        no verdict was recorded".
        """
        response = self.client.get(
            reverse("document-classification", args=[self.document.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_classification_returns_every_verdict_with_its_model(self):
        for version_label, label in (("v0.1.0", "REGULATION"), ("v0.2.0", "POLICY")):
            ClassificationResult.objects.create(
                document_version=self.version,
                document_content=self.content,
                model_name="text-baseline",
                model_version=version_label,
                predicted_label=label,
                confidence=0.71,
                metadata={"scores": {label: 0.71}},
            )

        response = self.client.get(
            reverse("document-classification", args=[self.document.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Both runs, not just the newest - a verdict is only interpretable
        # next to the model that produced it, and the comparison is the
        # reason both rows are kept.
        self.assertEqual(len(response.data), 2)
        self.assertEqual(
            {result["model_version"] for result in response.data},
            {"v0.1.0", "v0.2.0"},
        )
        self.assertEqual(response.data[0]["metadata"], {"scores": {"POLICY": 0.71}})

    # --- extraction ------------------------------------------------------

    def test_extraction_returns_fields_that_were_not_found_too(self):
        """A NOT_FOUND row is the denominator recall needs."""
        result = ExtractionResult.objects.create(
            document_version=self.version,
            document_content=self.content,
            model_name="rule-extractor",
            model_version="v1",
            schema_name="institutional",
            schema_version="v1",
            document_type=Document.DocumentType.REGULATION,
            field_count=2,
            found_count=1,
        )
        ExtractedField.objects.create(
            result=result,
            field_name="effective_date",
            status=ExtractedField.Status.FOUND,
            value="01/07/2024",
            confidence=0.9,
            page_number=1,
            character_start=10,
            character_end=20,
            matched_text="01/07/2024",
        )
        ExtractedField.objects.create(
            result=result,
            field_name="reference_number",
            status=ExtractedField.Status.NOT_FOUND,
        )

        response = self.client.get(
            reverse("document-extraction", args=[self.document.id])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        fields = response.data[0]["extracted_fields"]
        self.assertEqual(
            {field["field_name"]: field["status"] for field in fields},
            {
                "effective_date": ExtractedField.Status.FOUND,
                "reference_number": ExtractedField.Status.NOT_FOUND,
            },
        )

        found = next(field for field in fields if field["status"] == "FOUND")
        # The span travels with the value; without it the value is a claim
        # nobody can check against the stored page text.
        self.assertEqual(
            (found["page_number"], found["character_start"], found["character_end"]),
            (1, 10, 20),
        )

    # --- chunks ----------------------------------------------------------

    def test_chunks_carry_their_page_and_span(self):
        response = self.client.get(reverse("document-chunks", args=[self.document.id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        chunk = response.data["results"][0]
        page = self.content.pages.get(page_number=chunk["page_number"])

        # The same invariant the indexing service enforces: the offsets
        # re-slice out of the stored page text to the chunk's own text.
        self.assertEqual(
            page.text[chunk["character_start"] : chunk["character_end"]],
            chunk["text"],
        )

    def test_chunks_report_an_embedding_without_shipping_the_vector(self):
        chunk = DocumentChunk.objects.filter(document_content=self.content).first()
        DocumentChunkEmbedding.objects.create(
            chunk=chunk,
            model_name="hashing-baseline",
            model_version="v1",
            dimensions=384,
            semantic=False,
            embedding=[0.0] * 384,
        )

        response = self.client.get(reverse("document-chunks", args=[self.document.id]))
        embedding = response.data["results"][0]["embeddings"][0]

        # `semantic` is the part that changes how a result should be read.
        self.assertFalse(embedding["semantic"])
        self.assertEqual(embedding["dimensions"], 384)
        # 384 floats per chunk is a payload no reader can use, and shipping
        # it would make the response size scale with the index.
        self.assertNotIn("embedding", embedding)

    def test_chunks_are_404_when_nothing_has_been_extracted(self):
        self.version.contents.all().delete()

        response = self.client.get(reverse("document-chunks", args=[self.document.id]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ResultEndpointAuthorizationTests(AuthenticatedAPITestCase):
    """Derived output is the document's substance, so it is behind the same
    boundary. A field value quotes the document and a chunk is a verbatim
    slice of it - reading either without being allowed to read the document
    would make these endpoints the cheapest way around the access policy.
    """

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.outsider = make_user(
            "mech_faculty",
            role=UserProfile.Role.FACULTY,
            department="Mechanical",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": "CS Attendance Policy", "file": make_txt_file()},
                format="multipart",
            )

        self.document_id = response.data["id"]

    def test_another_department_is_denied_every_result_endpoint(self):
        self.client.force_authenticate(user=self.outsider)

        for route in ("document-classification", "document-extraction", "document-chunks"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.document_id]))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_denied_read_is_audited(self):
        self.client.force_authenticate(user=self.outsider)
        self.client.get(reverse("document-chunks", args=[self.document_id]))

        entry = AuditLog.objects.filter(user=self.outsider).latest("id")

        self.assertEqual(entry.action, AuditLog.Action.DOCUMENT_VIEW)
        self.assertEqual(entry.result, AuditLog.Result.DENIED)

    def test_an_allowed_read_is_audited_as_a_view(self):
        self.client.get(reverse("document-extraction", args=[self.document_id]))

        entry = AuditLog.objects.filter(user=self.user).latest("id")

        self.assertEqual(entry.action, AuditLog.Action.DOCUMENT_VIEW)
        self.assertEqual(entry.result, AuditLog.Result.ALLOWED)
