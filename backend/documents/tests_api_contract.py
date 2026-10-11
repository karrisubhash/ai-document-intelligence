"""API contract (spec 16): the versioned prefix, and idempotent uploads."""

import shutil

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents.models import Document, DocumentVersion, ProcessingJob
from documents.tests import MEDIA_ROOT, make_user

TEXT = b"Idempotency fixture text.\n"


class VersionedPrefixTests(APITestCase):
    def setUp(self):
        self.user = make_user("versioned", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=self.user)

    def test_v1_and_legacy_prefix_serve_the_same_endpoint(self):
        for path in ("/api/v1/documents/", "/api/documents/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, status.HTTP_200_OK)

    def test_reverse_keeps_the_legacy_paths(self):
        # Existing clients, tests and links were built from reverse(); the
        # alias is mounted last precisely so this does not change.
        self.assertEqual(reverse("document-list"), "/api/documents/")

    def test_v1_requires_authentication_like_the_alias(self):
        self.client.force_authenticate(user=None)
        self.assertIn(
            self.client.get("/api/v1/documents/").status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class IdempotentUploadTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.user = make_user("idem", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=self.user)

    def upload(
        self, key=None, title="Idempotent", content=TEXT, path="/api/v1/documents/"
    ):
        headers = {"Idempotency-Key": key} if key is not None else {}
        return self.client.post(
            path,
            {"title": title, "file": SimpleUploadedFile("note.txt", content)},
            format="multipart",
            headers=headers,
        )

    def test_retry_with_the_same_key_returns_the_first_document(self):
        first = self.upload("key-1")
        second = self.upload("key-1")
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(second["Idempotent-Replayed"], "true")
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(ProcessingJob.objects.count(), 1)

    def test_without_a_key_every_post_creates(self):
        self.upload()
        self.upload()
        self.assertEqual(Document.objects.count(), 2)

    def test_reusing_a_key_for_a_different_request_is_refused(self):
        self.upload("key-2", title="One")
        response = self.upload("key-2", title="Two")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Document.objects.count(), 1)

    def test_keys_are_scoped_per_user(self):
        self.upload("shared")
        other = make_user("idem-other", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=other)
        response = self.upload("shared")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotIn("Idempotent-Replayed", response)
        self.assertEqual(Document.objects.count(), 2)

    def test_a_refused_upload_releases_its_key(self):
        refused = self.upload("key-3", content=b"")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        accepted = self.upload("key-3")
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotIn("Idempotent-Replayed", accepted)

    def test_a_malformed_key_is_a_400(self):
        response = self.upload("x" * 300)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Document.objects.exists())

    def test_new_version_retry_creates_one_version(self):
        document_id = self.upload().data["id"]
        path = f"/api/v1/documents/{document_id}/versions/"
        first = self.client.post(
            path,
            {"file": SimpleUploadedFile("v2.txt", b"second version\n")},
            format="multipart",
            headers={"Idempotency-Key": "v2"},
        )
        second = self.client.post(
            path,
            {"file": SimpleUploadedFile("v2.txt", b"second version\n")},
            format="multipart",
            headers={"Idempotency-Key": "v2"},
        )
        self.assertEqual(first.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(
            DocumentVersion.objects.filter(document_id=document_id).count(), 2
        )
