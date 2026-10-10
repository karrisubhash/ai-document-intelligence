"""Admission control on upload (§13, §18).

The pipeline has always refused an oversized, empty or unparseable file.
What it could not do is refuse it in time: those checks run in the Celery
worker, minutes after the request returned 202 and after the bytes were
already written to MEDIA_ROOT or PUT into the bucket. The document was
marked FAILED and every artifact stayed.

These tests fix the boundary in place: what is refused, with which status,
and - the assertion that actually matters - that nothing was stored.
"""

import shutil

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents.models import AuditLog, Document, DocumentVersion, ProcessingJob
from documents.tests import MEDIA_ROOT, make_user

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer<</Root 1 0 R>>\n%%EOF\n"


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class UploadAdmissionTests(APITestCase):
    """POST /api/documents/ - the new-document path."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.user = make_user("uploader", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=self.user)

    def post(self, filename, content, title="Admission Fixture"):
        return self.client.post(
            reverse("document-list"),
            {"title": title, "file": SimpleUploadedFile(filename, content)},
            format="multipart",
        )

    def assertNothingWasStored(self):
        """The assertion the whole change exists for.

        A 4xx that still left a row and a file behind would be worse than
        the 202 it replaced - it would look like a refusal and cost the same
        as an acceptance.
        """
        self.assertFalse(Document.objects.exists(), "a Document row survived")
        self.assertFalse(DocumentVersion.objects.exists(), "a version row survived")
        self.assertFalse(ProcessingJob.objects.exists(), "a job was queued")

    # --- Size -------------------------------------------------------------

    @override_settings(MAX_DOCUMENT_SIZE_BYTES=64)
    def test_an_oversized_upload_is_refused_with_413(self):
        response = self.post("big.pdf", b"x" * 65)

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertIn("64", response.data["detail"])
        self.assertNothingWasStored()

    @override_settings(MAX_DOCUMENT_SIZE_BYTES=64)
    def test_a_file_exactly_at_the_limit_is_accepted(self):
        """An off-by-one here would reject a document the operator was told
        was allowed, which is the kind of limit nobody trusts afterwards."""
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post("ok.txt", b"x" * 64)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_an_empty_file_is_refused(self):
        """DRF's own FileField gets here first - `allow_empty_file` is False
        by default - so this asserts the outcome, not which check produced
        it. `validate_upload` keeps its own emptiness test for callers that
        are not a DRF field; see UploadValidatorTests."""
        response = self.post("empty.txt", b"")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNothingWasStored()

    # --- Type -------------------------------------------------------------

    def test_an_unsupported_extension_is_refused(self):
        response = self.post("payload.exe", b"MZ\x90\x00binary")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Unsupported file type", str(response.data))
        self.assertNothingWasStored()

    def test_an_archive_is_refused(self):
        """A .zip has DOCX's magic bytes, so INGESTION has to open it to
        find out what it is. Refusing the extension means it never gets
        stored to be opened."""
        response = self.post("bundle.zip", b"PK\x03\x04" + b"\x00" * 64)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNothingWasStored()

    def test_a_file_with_no_extension_is_refused(self):
        response = self.post("regulations", PDF)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNothingWasStored()

    def test_extension_matching_is_case_insensitive(self):
        """Windows uploads .PDF. Refusing it would be a bug that only
        appears for half the institution."""
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post("REGULATIONS.PDF", PDF)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_double_extension_is_judged_by_the_last_one(self):
        """`report.pdf.exe` is an executable. Reading the first extension is
        the classic way to get that wrong."""
        response = self.post("report.pdf.exe", b"MZ\x90\x00")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNothingWasStored()

    def test_a_supported_file_still_uploads(self):
        """The regression guard: an admission check that refuses everything
        passes every test above."""
        with self.captureOnCommitCallbacks(execute=False):
            response = self.post("regulations.pdf", PDF)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(DocumentVersion.objects.count(), 1)

    def test_an_overlong_client_content_type_does_not_fail_the_upload(self):
        """The part's Content-Type is the client's to write, at any length,
        and past mime_type's 100 characters the insert raised DataError."""
        upload = SimpleUploadedFile(
            "regulations.pdf", PDF, content_type="application/" + "x" * 150
        )
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                reverse("document-list"),
                {"title": "Admission Fixture", "file": upload},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(len(DocumentVersion.objects.get().mime_type), 100)

    # --- The other door ---------------------------------------------------

    def test_the_version_endpoint_enforces_the_same_rule(self):
        """A rule on one upload path and not the other is not a rule."""
        with self.captureOnCommitCallbacks(execute=False):
            created = self.post("regulations.pdf", PDF)

        document_id = created.data["id"]
        response = self.client.post(
            reverse("document-versions", args=[document_id]),
            {"file": SimpleUploadedFile("payload.exe", b"MZ\x90\x00")},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DocumentVersion.objects.count(), 1)

    @override_settings(MAX_DOCUMENT_SIZE_BYTES=64)
    def test_the_version_endpoint_enforces_the_size_limit(self):
        with self.captureOnCommitCallbacks(execute=False):
            created = self.post("regulations.txt", b"short")

        response = self.client.post(
            reverse("document-versions", args=[created.data["id"]]),
            {"file": SimpleUploadedFile("big.txt", b"x" * 65)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(DocumentVersion.objects.count(), 1)

    # --- Trail ------------------------------------------------------------

    def test_a_refusal_is_audited_without_recording_the_file(self):
        """An operator needs to see a pattern of refusals. They must not
        find the document's contents in the audit table while doing it."""
        self.post("payload.exe", b"MZ\x90\x00secret-content")

        entry = AuditLog.objects.get()
        self.assertEqual(entry.action, AuditLog.Action.DOCUMENT_CREATE)
        self.assertEqual(entry.result, AuditLog.Result.DENIED)
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.metadata["reason"], "unsupported_extension")
        self.assertEqual(entry.metadata["file_name"], "payload.exe")
        self.assertNotIn("secret-content", str(entry.metadata))

    @override_settings(MAX_DOCUMENT_SIZE_BYTES=64)
    def test_an_oversize_refusal_records_both_numbers(self):
        """Without the limit beside the size, the audit line cannot be read
        six months later when the setting has changed."""
        self.post("big.txt", b"x" * 100)

        entry = AuditLog.objects.get()
        self.assertEqual(entry.metadata["reason"], "too_large")
        self.assertEqual(entry.metadata["size_bytes"], 100)
        self.assertEqual(entry.metadata["limit_bytes"], 64)

    def test_a_refusal_is_counted(self):
        from documents.observability import metrics

        def total():
            for metric in metrics.REGISTRY.collect():
                if metric.name == "adi_upload_rejections":
                    return sum(
                        sample.value
                        for sample in metric.samples
                        if sample.labels.get("reason") == "unsupported_extension"
                    )
            return 0.0

        before = total()
        self.post("payload.exe", b"MZ\x90\x00")

        self.assertEqual(total(), before + 1)

    # --- Boundary, not authorization --------------------------------------

    def test_an_anonymous_upload_is_still_refused_by_authentication(self):
        """The admission check must not become the first thing that runs.
        An unauthenticated caller should never reach it - otherwise a 413
        tells an anonymous prober what the platform's size limit is."""
        self.client.force_authenticate(user=None)

        response = self.post("payload.exe", b"MZ\x90\x00")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertFalse(AuditLog.objects.exists())


class UploadValidatorTests(APITestCase):
    """`validate_upload` directly, for the paths no HTTP request takes."""

    def test_an_empty_file_is_refused_without_a_serializer(self):
        from rest_framework.exceptions import ValidationError

        from documents.uploads import validate_upload

        with self.assertRaises(ValidationError):
            validate_upload(SimpleUploadedFile("empty.txt", b""))

    def test_the_supported_set_follows_the_pipeline(self):
        """One list, not two. If INGESTION learns a new format, the boundary
        accepts it in the same commit - a second hardcoded set is how a
        format ends up parseable and unuploadable."""
        from documents.processing.stages.ingestion import EXTENSION_TYPES
        from documents.uploads import SUPPORTED_EXTENSIONS

        self.assertEqual(SUPPORTED_EXTENSIONS, frozenset(EXTENSION_TYPES))

    def test_it_returns_the_file_it_was_given(self):
        """It is a serializer field validator, so its return value is what
        gets stored. Returning None here would silently drop the upload."""
        from documents.uploads import validate_upload

        upload = SimpleUploadedFile("a.txt", b"body")
        self.assertIs(validate_upload(upload), upload)
