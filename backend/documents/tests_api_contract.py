"""API contract (spec 16): the versioned prefix, and idempotent uploads."""

import shutil
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents import idempotency
from documents.models import AuditLog, Document, DocumentVersion, ProcessingJob
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
        self,
        key=None,
        title="Idempotent",
        content=TEXT,
        path="/api/v1/documents/",
        **fields,
    ):
        headers = {"Idempotency-Key": key} if key is not None else {}
        return self.client.post(
            path,
            {"title": title, "file": SimpleUploadedFile("note.txt", content), **fields},
            format="multipart",
            headers=headers,
        )

    def upload_version(self, document_id, key, content=b"second version\n"):
        return self.client.post(
            f"/api/v1/documents/{document_id}/versions/",
            {"file": SimpleUploadedFile("v2.txt", content)},
            format="multipart",
            headers={"Idempotency-Key": key},
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
        # Refused AFTER the key is claimed: the department is checked inside
        # the upload. (An empty file, as this test used to send, is refused
        # by the serializer before any claim, so it passed whether or not a
        # key was ever released.) Held, the key would answer the corrected
        # retry - a different request - with 422.
        admin = make_user("idem-admin", UserProfile.Role.SYSTEM_ADMIN, "")
        self.client.force_authenticate(user=admin)
        refused = self.upload("key-3", department="No Such Department")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("department", refused.data)
        accepted = self.upload("key-3")
        self.assertEqual(accepted.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotIn("Idempotent-Replayed", accepted)

    def test_a_different_file_of_the_same_name_and_size_is_refused(self):
        # By name and size alone this was "the same request": the edited
        # file was dropped and the first upload's result came back.
        first = b"The fee is 4,000 per term.\n"
        edited = b"The fee is 5,000 per term.\n"
        self.assertEqual(len(first), len(edited))
        self.upload("key-4", content=first)
        response = self.upload("key-4", content=edited)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(Document.objects.count(), 1)

    def test_a_retry_while_the_first_is_running_is_a_409(self):
        request = SimpleNamespace(headers={"Idempotency-Key": "key-5"}, user=self.user)
        first = idempotency.Idempotency(request, "documents.create", "same")
        self.assertIsNone(first.start())
        with self.assertRaises(idempotency.IdempotencyConflict):
            idempotency.Idempotency(request, "documents.create", "same").start()
        first.finish(42)
        retry = idempotency.Idempotency(request, "documents.create", "same")
        self.assertEqual(retry.start(), 42)

    def test_a_claim_nobody_finished_does_not_hold_the_key_for_a_day(self):
        # A worker killed mid-upload never releases its claim; retries got
        # 409 for the claim's whole lifetime, which was the full 24 hours.
        with mock.patch.object(
            idempotency.cache, "add", wraps=idempotency.cache.add
        ) as add:
            self.upload("key-6")
        self.assertEqual(add.call_args.args[2], idempotency.PENDING_TTL_SECONDS)
        self.assertLessEqual(idempotency.PENDING_TTL_SECONDS, 15 * 60)

    def test_without_a_key_the_upload_is_not_read_for_a_fingerprint(self):
        with mock.patch.object(idempotency, "fingerprint") as fingerprint:
            self.assertEqual(self.upload().status_code, status.HTTP_202_ACCEPTED)
        fingerprint.assert_not_called()

    def test_a_version_upload_is_audited_once_however_often_it_is_retried(self):
        document_id = self.upload().data["id"]
        self.upload_version(document_id, "v2")
        self.upload_version(document_id, "v2")
        rows = AuditLog.objects.filter(
            action=AuditLog.Action.DOCUMENT_VERSION_UPLOAD, document_id=document_id
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().result, AuditLog.Result.ALLOWED)
        self.assertEqual(rows.get().metadata["version_number"], 2)

    def test_an_upload_refused_for_an_archived_document_is_not_audited_as_one(self):
        document_id = self.upload().data["id"]
        self.client.post(f"/api/v1/documents/{document_id}/archive/")
        response = self.upload_version(document_id, "v2-archived")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(
            AuditLog.objects.filter(
                action=AuditLog.Action.DOCUMENT_VERSION_UPLOAD
            ).exists()
        )

    def test_a_denied_version_upload_is_still_audited(self):
        document_id = self.upload().data["id"]
        reader = make_user("idem-reader", UserProfile.Role.STUDENT, "Computer Science")
        self.client.force_authenticate(user=reader)
        response = self.upload_version(document_id, "v2-denied")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        row = AuditLog.objects.get(
            action=AuditLog.Action.DOCUMENT_VERSION_UPLOAD, document_id=document_id
        )
        self.assertEqual(row.result, AuditLog.Result.DENIED)

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
