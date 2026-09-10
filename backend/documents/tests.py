import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents.models import AuditLog, Document, DocumentVersion, ProcessingEvent, ProcessingJob
from documents.processing.errors import StageError

User = get_user_model()

MEDIA_ROOT = tempfile.mkdtemp(prefix="documents-tests-")


def make_txt_file(name="regulations.txt", content=b"Minimum attendance is 75 percent."):
    return SimpleUploadedFile(name, content, content_type="text/plain")


def make_user(username, role=UserProfile.Role.FACULTY, department="Computer Science"):
    user = User.objects.create_user(username=username, password="pass1234")
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


class AuthenticatedAPITestCase(APITestCase):
    """Base for tests that only care about document behavior, not auth
    itself - logs in a single ordinary FACULTY user so the M2-era
    behavior can be exercised unchanged under M3's auth requirement."""

    def setUp(self):
        super().setUp()
        self.user = make_user("faculty1")
        self.client.force_authenticate(user=self.user)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentUploadTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_upload_returns_202_with_queued_status(self):
        url = reverse("document-list")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "title": "Academic Regulations",
                    "file": make_txt_file(),
                },
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["title"], "Academic Regulations")
        # By the time the task has run synchronously (eager mode), the
        # document has already progressed to COMPLETED - the important
        # contract is that the HTTP layer didn't block on processing and
        # a job/version were created as part of the same request.
        self.assertIn(response.data["status"], [Document.Status.QUEUED, Document.Status.COMPLETED])

        document = Document.objects.get(id=response.data["id"])
        self.assertEqual(document.versions.count(), 1)
        self.assertEqual(document.versions.first().version_number, 1)
        self.assertEqual(ProcessingJob.objects.filter(document_version__document=document).count(), 1)

    def test_upload_requires_title(self):
        url = reverse("document-list")

        response = self.client.post(
            url,
            {"file": make_txt_file()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", response.data)

    def test_upload_requires_file(self):
        url = reverse("document-list")

        response = self.client.post(
            url,
            {"title": "Missing File"},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", response.data)

    def test_request_does_not_wait_for_processing(self):
        """The Document must already exist as QUEUED before the Celery
        task has any chance to run - i.e. creation happens synchronously
        in the request, processing does not."""
        url = reverse("document-list")

        response = self.client.post(
            url,
            {"title": "Academic Regulations", "file": make_txt_file()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        document = Document.objects.get(id=response.data["id"])
        # No on_commit callbacks were executed (no captureOnCommitCallbacks
        # here), so the eager task has not run yet: the document must
        # still be sitting at QUEUED, proving the HTTP path never blocked
        # on pipeline execution.
        self.assertEqual(document.status, Document.Status.QUEUED)
        self.assertEqual(
            document.versions.first().processing_jobs.first().status,
            ProcessingJob.Status.QUEUED,
        )


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentStatusTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_get_document_reflects_completed_processing(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        document_id = create_response.data["id"]
        response = self.client.get(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], Document.Status.COMPLETED)
        self.assertIsNotNone(response.data["current_version"])
        self.assertEqual(response.data["current_version"]["version_number"], 1)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentVersioningTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_uploading_new_version_extends_same_document(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        document_id = create_response.data["id"]

        with self.captureOnCommitCallbacks(execute=True):
            version_response = self.client.post(
                reverse("document-versions", args=[document_id]),
                {"file": make_txt_file(name="regulations_v2.txt", content=b"Minimum attendance is 80 percent.")},
                format="multipart",
            )

        self.assertEqual(version_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(version_response.data["version_number"], 2)

        # Still exactly one Document - not a second unrelated document.
        self.assertEqual(Document.objects.count(), 1)
        document = Document.objects.get(id=document_id)
        self.assertEqual(document.versions.count(), 2)
        self.assertEqual(document.current_version.version_number, 2)

        # Original version's file is untouched - both versions coexist.
        v1, v2 = document.versions.order_by("version_number")
        self.assertNotEqual(v1.file.name, v2.file.name)

    def test_versions_endpoint_lists_all_versions(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        document_id = create_response.data["id"]
        response = self.client.get(reverse("document-versions", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentProcessingEndpointTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_processing_endpoint_reports_job_and_events(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        document_id = create_response.data["id"]
        response = self.client.get(reverse("document-processing", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        job = response.data[0]
        self.assertEqual(job["status"], ProcessingJob.Status.COMPLETED)
        self.assertEqual(
            [event["stage"] for event in job["events"]],
            [
                ProcessingJob.Stage.INGESTION,
                ProcessingJob.Stage.VALIDATION,
                ProcessingJob.Stage.TEXT_EXTRACTION,
                ProcessingJob.Stage.OCR,
                ProcessingJob.Stage.CLASSIFICATION,
            ],
        )
        # CLASSIFICATION is SKIPPED while no model is configured; nothing failed.
        self.assertNotIn(
            ProcessingEvent.Status.FAILED,
            [event["status"] for event in job["events"]],
        )


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentReprocessTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_reprocess_creates_a_new_job_for_the_same_version(self):
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Academic Regulations", "file": make_txt_file()},
                format="multipart",
            )

        document_id = create_response.data["id"]

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("document-reprocess", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        document = Document.objects.get(id=document_id)
        self.assertEqual(document.versions.count(), 1)
        self.assertEqual(
            ProcessingJob.objects.filter(document_version__document=document).count(), 2
        )
        self.assertEqual(document.status, Document.Status.COMPLETED)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class PipelineFailureTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_empty_document_fails_without_destroying_document(self):
        # A permanent failure propagates as itself rather than a Celery
        # Retry - the task does not re-attempt a document that cannot
        # succeed. See DocumentFailureTaxonomyTests.
        with self.assertRaises(StageError):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("document-list"),
                    {
                        "title": "Blank Circular",
                        "file": make_txt_file(name="blank.txt", content=b"   "),
                    },
                    format="multipart",
                )

        document_id = response.data["id"]
        document = Document.objects.get(id=document_id)
        self.assertEqual(document.status, Document.Status.FAILED)

        job = ProcessingJob.objects.get(document_version__document=document)
        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(job.error_code, "EMPTY_EXTRACTION")
        self.assertTrue(job.error_message)

        # The document and its version are preserved, not deleted.
        self.assertTrue(Document.objects.filter(id=document_id).exists())
        self.assertEqual(document.versions.count(), 1)

    def test_unsupported_file_type_fails_validation(self):
        # Since ingestion v1 this is caught by the VALIDATION stage rather
        # than surfacing as a generic extraction failure - the file never
        # reaches an extractor.
        with self.assertRaises(StageError):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("document-list"),
                    {
                        "title": "Weird Format",
                        "file": SimpleUploadedFile(
                            "notes.xyz", b"binary-ish content", content_type="application/octet-stream"
                        ),
                    },
                    format="multipart",
                )

        document = Document.objects.get(id=response.data["id"])
        job = ProcessingJob.objects.get(document_version__document=document)

        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertEqual(job.error_code, "UNSUPPORTED_FILE_TYPE")
        self.assertEqual(job.current_stage, ProcessingJob.Stage.VALIDATION)


class AuthenticationTests(APITestCase):
    """Nothing in the document API is reachable without authenticating."""

    def test_list_requires_authentication(self):
        response = self.client.get(reverse("document-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_requires_authentication(self):
        response = self.client.post(
            reverse("document-list"),
            {"title": "Sneaky Upload", "file": make_txt_file()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(Document.objects.count(), 0)

    def test_detail_requires_authentication(self):
        response = self.client.get(reverse("document-detail", args=[37]))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_token_endpoint_issues_a_token(self):
        make_user("faculty1")
        response = self.client.post(
            reverse("api-token-auth"),
            {"username": "faculty1", "password": "pass1234"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("token", response.data)

    def test_token_endpoint_rejects_bad_credentials(self):
        make_user("faculty1")
        response = self.client.post(
            reverse("api-token-auth"),
            {"username": "faculty1", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class DocumentAuthorizationTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.cs_faculty = make_user("cs_faculty", UserProfile.Role.FACULTY, "Computer Science")
        self.cs_faculty_other = make_user(
            "cs_faculty_2", UserProfile.Role.FACULTY, "Computer Science"
        )
        self.cs_dept_admin = make_user(
            "cs_admin", UserProfile.Role.DEPARTMENT_ADMIN, "Computer Science"
        )
        self.mech_faculty = make_user(
            "mech_faculty", UserProfile.Role.FACULTY, "Mechanical Engineering"
        )
        self.system_admin = make_user("sys_admin", UserProfile.Role.SYSTEM_ADMIN, "")

    def upload_as(self, user, title="Departmental Policy"):
        self.client.force_authenticate(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": title, "file": make_txt_file()},
                format="multipart",
            )
        self.client.force_authenticate(user=None)
        return response.data["id"]

    def test_owner_can_view_own_document(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.cs_faculty)
        response = self.client.get(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_same_department_colleague_can_view(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.cs_faculty_other)
        response = self.client.get(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_other_department_user_cannot_view(self):
        """Knowing the id is not enough - object-level check must deny."""
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.mech_faculty)
        response = self.client.get(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_system_admin_can_view_any_department(self):
        document_id = self.upload_as(self.mech_faculty)

        self.client.force_authenticate(user=self.system_admin)
        response = self.client.get(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_list_is_scoped_to_own_department(self):
        self.upload_as(self.cs_faculty, title="CS Policy")
        self.upload_as(self.mech_faculty, title="Mech Policy")

        self.client.force_authenticate(user=self.cs_faculty)
        response = self.client.get(reverse("document-list"))

        titles = [document["title"] for document in response.data]
        self.assertIn("CS Policy", titles)
        self.assertNotIn("Mech Policy", titles)

    def test_system_admin_list_spans_departments(self):
        self.upload_as(self.cs_faculty, title="CS Policy")
        self.upload_as(self.mech_faculty, title="Mech Policy")

        self.client.force_authenticate(user=self.system_admin)
        response = self.client.get(reverse("document-list"))

        titles = [document["title"] for document in response.data]
        self.assertIn("CS Policy", titles)
        self.assertIn("Mech Policy", titles)

    def test_department_admin_can_update_department_document(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.cs_dept_admin)
        response = self.client.patch(
            reverse("document-detail", args=[document_id]), {"title": "Renamed"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_colleague_can_read_but_not_modify(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.cs_faculty_other)
        response = self.client.patch(
            reverse("document-detail", args=[document_id]), {"title": "Hijacked"}
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_department_user_cannot_delete(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.mech_faculty)
        response = self.client.delete(reverse("document-detail", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Document.objects.filter(id=document_id).exists())

    def test_other_department_user_cannot_reprocess(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.mech_faculty)
        response = self.client.post(reverse("document-reprocess", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_department_user_cannot_list_versions(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.mech_faculty)
        response = self.client.get(reverse("document-versions", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_department_user_cannot_download(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.mech_faculty)
        response = self.client.get(reverse("document-download", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_authorized_user_can_download(self):
        document_id = self.upload_as(self.cs_faculty)

        self.client.force_authenticate(user=self.cs_faculty)
        response = self.client.get(reverse("document-download", args=[document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_raw_media_path_is_not_publicly_served(self):
        """The stored file must not be reachable outside the authorized,
        audited download endpoint - serving MEDIA_ROOT directly would
        hand the bytes to anonymous callers and bypass every check."""
        document_id = self.upload_as(self.cs_faculty)
        version = Document.objects.get(id=document_id).current_version

        response = self.client.get(f"/media/{version.file.name}")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class UploadOwnershipTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def test_ownership_and_department_are_server_assigned(self):
        user = make_user("cs_faculty", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": "Departmental Policy",
                    # Both are attempts to spoof server-controlled fields.
                    "department": "Mechanical Engineering",
                    "owner": 9999,
                    "file": make_txt_file(),
                },
                format="multipart",
            )

        document = Document.objects.get(id=response.data["id"])
        self.assertEqual(document.owner, user)
        self.assertEqual(document.department, "Computer Science")
        self.assertEqual(document.versions.first().uploaded_by, user)

    def test_system_admin_may_set_department_explicitly(self):
        admin = make_user("sys_admin", UserProfile.Role.SYSTEM_ADMIN, "")
        self.client.force_authenticate(user=admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": "Cross-department Circular",
                    "department": "Mechanical Engineering",
                    "file": make_txt_file(),
                },
                format="multipart",
            )

        document = Document.objects.get(id=response.data["id"])
        self.assertEqual(document.department, "Mechanical Engineering")

    def test_new_version_records_the_uploading_user(self):
        owner = make_user("cs_faculty", UserProfile.Role.FACULTY, "Computer Science")
        dept_admin = make_user("cs_admin", UserProfile.Role.DEPARTMENT_ADMIN, "Computer Science")

        self.client.force_authenticate(user=owner)
        with self.captureOnCommitCallbacks(execute=True):
            create_response = self.client.post(
                reverse("document-list"),
                {"title": "Departmental Policy", "file": make_txt_file()},
                format="multipart",
            )
        document_id = create_response.data["id"]

        self.client.force_authenticate(user=dept_admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("document-versions", args=[document_id]),
                {"file": make_txt_file(name="v2.txt", content=b"Updated policy text.")},
                format="multipart",
            )

        version_2 = DocumentVersion.objects.get(
            document_id=document_id, version_number=2
        )
        self.assertEqual(version_2.uploaded_by, dept_admin)


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class AuditLogTests(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.owner = make_user("cs_faculty", UserProfile.Role.FACULTY, "Computer Science")
        self.outsider = make_user(
            "mech_faculty", UserProfile.Role.FACULTY, "Mechanical Engineering"
        )

        self.client.force_authenticate(user=self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": "Internal HR Policy", "file": make_txt_file()},
                format="multipart",
            )
        self.document_id = response.data["id"]

    def test_upload_is_audited(self):
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.owner,
                action=AuditLog.Action.DOCUMENT_CREATE,
                document_id=self.document_id,
                result=AuditLog.Result.ALLOWED,
            ).exists()
        )

    def test_allowed_view_is_audited(self):
        self.client.force_authenticate(user=self.owner)
        self.client.get(reverse("document-detail", args=[self.document_id]))

        self.assertTrue(
            AuditLog.objects.filter(
                user=self.owner,
                action=AuditLog.Action.DOCUMENT_VIEW,
                document_id=self.document_id,
                result=AuditLog.Result.ALLOWED,
            ).exists()
        )

    def test_denied_view_is_audited(self):
        self.client.force_authenticate(user=self.outsider)
        response = self.client.get(reverse("document-detail", args=[self.document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.outsider,
                action=AuditLog.Action.DOCUMENT_VIEW,
                document_id=self.document_id,
                result=AuditLog.Result.DENIED,
            ).exists()
        )

    def test_denied_download_is_audited(self):
        self.client.force_authenticate(user=self.outsider)
        response = self.client.get(reverse("document-download", args=[self.document_id]))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.outsider,
                action=AuditLog.Action.DOCUMENT_DOWNLOAD,
                document_id=self.document_id,
                result=AuditLog.Result.DENIED,
            ).exists()
        )

    def test_denied_delete_is_audited(self):
        self.client.force_authenticate(user=self.outsider)
        self.client.delete(reverse("document-detail", args=[self.document_id]))

        self.assertTrue(
            AuditLog.objects.filter(
                user=self.outsider,
                action=AuditLog.Action.DOCUMENT_DELETE,
                document_id=self.document_id,
                result=AuditLog.Result.DENIED,
            ).exists()
        )
