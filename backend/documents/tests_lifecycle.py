"""Document lifecycle: concurrency, stale jobs, archival, recovery, access.

Each class pins a failure that was reproducible before the change it tests:

* two uploads of a new version at once produced an IntegrityError and a 500;
* a job for a superseded version overwrote the current version's status;
* a redelivered COMPLETED job ran the whole pipeline again;
* a double-clicked Reprocess started two runs racing to be the live pass;
* a worker killed mid-job left the document at "Processing" forever;
* a hung OCR page was recorded as UNEXPECTED_ERROR, not as a timeout;
* a 403 confirmed that an id belonged to another department's document.
"""

import shutil
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import UserProfile
from documents import services
from documents.models import AuditLog, Document, ProcessingEvent, ProcessingJob
from documents.processing import pipeline
from documents.processing.errors import ErrorCode
from documents.processing.pipeline import run_pipeline
from documents.search import service as search_service
from documents.tasks import is_retryable
from documents.tests import MEDIA_ROOT, make_txt_file, make_user

EAGER = {
    "MEDIA_ROOT": MEDIA_ROOT,
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
}


def make_document(owner, title="Attendance Policy", content=None):
    """A document with one version and its queued job - no processing run."""
    document = Document.objects.create(
        title=title, owner=owner, department=owner.profile.department
    )
    with mock.patch.object(services.process_document_task, "delay"):
        services.create_document_version(
            document,
            make_txt_file(
                content=content or b"Minimum attendance is 75 percent for exams."
            ),
            uploaded_by=owner,
        )
    document.refresh_from_db()
    return document


def job_for(document):
    return ProcessingJob.objects.filter(
        document_version=document.current_version
    ).latest("created_at")


def age(job, minutes):
    ProcessingJob.objects.filter(pk=job.pk).update(
        updated_at=timezone.now() - timedelta(minutes=minutes)
    )


@override_settings(**EAGER)
class StaleJobStatusTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.owner = make_user("lifecycle_owner")

    def test_a_superseded_versions_job_does_not_set_the_documents_status(self):
        document = make_document(self.owner)
        first_job = job_for(document)

        # Version 2 arrives before version 1's job has run.
        with mock.patch.object(services.process_document_task, "delay"):
            services.create_document_version(
                document, make_txt_file(content=b"Version two text."), self.owner
            )

        run_pipeline(first_job.id)

        document.refresh_from_db()
        first_job.refresh_from_db()
        self.assertEqual(first_job.status, ProcessingJob.Status.COMPLETED)
        # The document describes version 2, which has not been processed.
        self.assertEqual(document.status, Document.Status.QUEUED)

    def test_the_current_versions_job_still_sets_it(self):
        document = make_document(self.owner)
        run_pipeline(job_for(document).id)

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.COMPLETED)

    def test_a_redelivered_completed_job_does_not_run_again(self):
        document = make_document(self.owner)
        job = job_for(document)
        run_pipeline(job.id)
        events = ProcessingEvent.objects.filter(processing_job=job).count()

        run_pipeline(job.id)

        self.assertEqual(
            ProcessingEvent.objects.filter(processing_job=job).count(), events
        )
        job.refresh_from_db()
        self.assertEqual(job.attempt, 1)

    def test_a_stalled_job_delivered_late_does_not_run(self):
        """It has been replaced; running it would race the replacement."""
        document = make_document(self.owner)
        job = job_for(document)
        ProcessingJob.objects.filter(pk=job.pk).update(
            status=ProcessingJob.Status.FAILED, error_code=ErrorCode.STALLED
        )

        run_pipeline(job.id)

        self.assertFalse(ProcessingEvent.objects.filter(processing_job=job).exists())

    def test_a_soft_time_limit_is_recorded_as_a_timeout_and_not_retried(self):
        document = make_document(self.owner)
        job = job_for(document)

        def hangs(context):
            raise SoftTimeLimitExceeded()

        # The stage list holds the functions it was built with, so the stage
        # is swapped there rather than on its module.
        stages = [
            (stage, hangs if stage == ProcessingJob.Stage.TEXT_EXTRACTION else fn)
            for stage, fn in pipeline.CONTENT_STAGES
        ]
        with mock.patch.object(pipeline, "CONTENT_STAGES", stages):
            with self.assertRaises(SoftTimeLimitExceeded) as raised:
                run_pipeline(job.id)

        job.refresh_from_db()
        self.assertEqual(job.error_code, ErrorCode.TIMEOUT)
        self.assertFalse(is_retryable(raised.exception))
        failed = ProcessingEvent.objects.get(
            processing_job=job, status=ProcessingEvent.Status.FAILED
        )
        self.assertEqual(failed.metadata["error_code"], ErrorCode.TIMEOUT)


@override_settings(**EAGER)
class ReprocessConcurrencyTests(APITestCase):
    def setUp(self):
        self.owner = make_user("reprocess_owner")
        self.client.force_authenticate(self.owner)
        self.document = make_document(self.owner)

    def reprocess(self):
        with mock.patch.object(services.process_document_task, "delay"):
            return self.client.post(
                reverse("document-reprocess", args=[self.document.id])
            )

    def test_reprocess_is_refused_while_a_run_is_queued(self):
        response = self.reprocess()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["job"]["id"], job_for(self.document).id)
        self.assertEqual(
            ProcessingJob.objects.filter(
                document_version=self.document.current_version
            ).count(),
            1,
        )

    def test_a_stalled_run_does_not_block_reprocessing(self):
        stale = job_for(self.document)
        age(stale, minutes=500)

        response = self.reprocess()

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        stale.refresh_from_db()
        self.assertEqual(stale.error_code, ErrorCode.STALLED)

    def test_reprocess_after_completion_is_allowed(self):
        ProcessingJob.objects.filter(pk=job_for(self.document).pk).update(
            status=ProcessingJob.Status.COMPLETED
        )
        self.assertEqual(self.reprocess().status_code, status.HTTP_202_ACCEPTED)


class ConcurrentVersionUploadTests(TransactionTestCase):
    """Real concurrency needs real transactions, so no TestCase here."""

    def test_concurrent_new_versions_get_distinct_numbers(self):
        owner = make_user("concurrent_owner")
        with override_settings(MEDIA_ROOT=MEDIA_ROOT):
            document = make_document(owner)

        errors = []
        barrier = threading.Barrier(3)

        def upload(index):
            try:
                barrier.wait()
                services.create_document_version(
                    Document.objects.get(pk=document.pk),
                    make_txt_file(content=f"Revision {index}".encode()),
                    owner,
                )
            except Exception as exc:  # recorded, then asserted on below
                errors.append(exc)
            finally:
                connection.close()

        # Patched ONCE, around every thread. mock.patch is not thread-safe:
        # three threads each patching and restoring the same attribute
        # interleave their restores, and one ends up deleting it from under
        # another.
        with (
            override_settings(MEDIA_ROOT=MEDIA_ROOT),
            mock.patch.object(services.process_document_task, "delay"),
        ):
            threads = [threading.Thread(target=upload, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        numbers = sorted(document.versions.values_list("version_number", flat=True))
        self.assertEqual(numbers, [1, 2, 3, 4])


@override_settings(**EAGER)
class StalledJobRecoveryTests(TestCase):
    def setUp(self):
        self.owner = make_user("recovery_owner")
        self.document = make_document(self.owner)
        self.job = job_for(self.document)
        ProcessingJob.objects.filter(pk=self.job.pk).update(
            status=ProcessingJob.Status.PROCESSING
        )
        Document.objects.filter(pk=self.document.pk).update(
            status=Document.Status.PROCESSING
        )

    def test_a_recent_job_is_left_alone(self):
        self.assertEqual(services.recover_stalled_jobs(), [])

    def test_an_old_processing_job_is_marked_stalled(self):
        age(self.job, minutes=500)

        recovered = services.recover_stalled_jobs()

        self.assertEqual([job.id for job, _ in recovered], [self.job.id])
        self.job.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(self.job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(self.job.error_code, ErrorCode.STALLED)
        self.assertEqual(self.document.status, Document.Status.FAILED)

    def test_a_long_queue_is_not_a_failure(self):
        ProcessingJob.objects.filter(pk=self.job.pk).update(
            status=ProcessingJob.Status.QUEUED
        )
        age(self.job, minutes=5000)

        self.assertEqual(services.recover_stalled_jobs(), [])

    def test_requeue_starts_a_fresh_run(self):
        age(self.job, minutes=500)

        with mock.patch.object(services.process_document_task, "delay"):
            with self.captureOnCommitCallbacks(execute=True):
                recovered = services.recover_stalled_jobs(requeue=True)

        replacement = recovered[0][1]
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.status, ProcessingJob.Status.QUEUED)
        self.assertNotEqual(replacement.id, self.job.id)

    def test_the_command_reports_without_changing_anything_by_default(self):
        age(self.job, minutes=500)
        out = StringIO()

        call_command("recover_stalled_jobs", stdout=out)

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ProcessingJob.Status.PROCESSING)
        self.assertIn("1 stalled job", out.getvalue())

    def test_the_command_applies_when_asked(self):
        age(self.job, minutes=500)
        call_command("recover_stalled_jobs", "--apply", stdout=StringIO())

        self.job.refresh_from_db()
        self.assertEqual(self.job.error_code, ErrorCode.STALLED)


@override_settings(**EAGER)
class ArchivalTests(APITestCase):
    def setUp(self):
        self.owner = make_user("archive_owner")
        self.colleague = make_user("archive_colleague")
        self.outsider = make_user("archive_outsider", department="Mechanical")
        self.client.force_authenticate(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {
                    "title": "Hostel Curfew",
                    "file": make_txt_file(
                        content=b"The hostel curfew is 10 pm on weekdays."
                    ),
                },
                format="multipart",
            )
        self.document_id = response.data["id"]

    def archive(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.owner)
        return client.post(reverse("document-archive", args=[self.document_id]))

    def listed_ids(self, **params):
        response = self.client.get(reverse("document-list"), params)
        return [row["id"] for row in response.data["results"]]

    def test_archiving_hides_it_from_the_default_listing(self):
        self.assertEqual(self.archive().status_code, status.HTTP_200_OK)

        self.assertNotIn(self.document_id, self.listed_ids())
        self.assertIn(self.document_id, self.listed_ids(archived="true"))
        self.assertIn(self.document_id, self.listed_ids(archived="all"))

    def test_an_archived_document_is_not_searchable(self):
        found = search_service.search(self.owner, "hostel curfew")
        self.assertTrue(found.hits)

        self.archive()

        self.assertEqual(search_service.search(self.owner, "hostel curfew").hits, [])

    def test_restoring_brings_it_back_without_reindexing(self):
        self.archive()
        restored = self.client.post(
            reverse("document-restore", args=[self.document_id])
        )

        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertFalse(restored.data["is_archived"])
        self.assertTrue(search_service.search(self.owner, "hostel curfew").hits)

    def test_an_archived_document_is_still_readable_by_those_who_could_read_it(self):
        self.archive()
        client = APIClient()
        client.force_authenticate(self.colleague)

        response = client.get(reverse("document-detail", args=[self.document_id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_archived"])

    def test_an_archived_document_accepts_no_new_version_or_reprocess(self):
        self.archive()

        version = self.client.post(
            reverse("document-versions", args=[self.document_id]),
            {"file": make_txt_file()},
            format="multipart",
        )
        reprocess = self.client.post(
            reverse("document-reprocess", args=[self.document_id])
        )

        self.assertEqual(version.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(reprocess.status_code, status.HTTP_409_CONFLICT)

    def test_a_colleague_may_not_archive_and_an_outsider_cannot_see_it(self):
        self.assertEqual(
            self.archive(self.colleague).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.archive(self.outsider).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_archive_and_restore_are_audited(self):
        self.archive()
        self.client.post(reverse("document-restore", args=[self.document_id]))

        actions = set(
            AuditLog.objects.filter(document_id=self.document_id).values_list(
                "action", flat=True
            )
        )
        self.assertIn(AuditLog.Action.DOCUMENT_ARCHIVE, actions)
        self.assertIn(AuditLog.Action.DOCUMENT_RESTORE, actions)


@override_settings(**EAGER)
class NoExistenceLeakTests(APITestCase):
    """An id you may not read must look exactly like an id that is unused."""

    def setUp(self):
        self.owner = make_user("leak_owner")
        self.outsider = make_user("leak_outsider", department="Mechanical")
        self.document = make_document(self.owner)
        self.client.force_authenticate(self.outsider)

    def test_the_response_is_identical_to_a_missing_id(self):
        missing = Document.objects.order_by("-id").first().id + 1000

        for route in (
            "document-detail",
            "document-content",
            "document-versions",
            "document-download",
            "document-tables",
            "document-figures",
        ):
            with self.subTest(route=route):
                hidden = self.client.get(reverse(route, args=[self.document.id]))
                absent = self.client.get(reverse(route, args=[missing]))

                self.assertEqual(hidden.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(absent.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(hidden.json(), absent.json())

    def test_write_actions_are_hidden_the_same_way(self):
        for method, route in (
            ("patch", "document-detail"),
            ("delete", "document-detail"),
            ("post", "document-reprocess"),
            ("post", "document-archive"),
        ):
            with self.subTest(route=route, method=method):
                response = getattr(self.client, method)(
                    reverse(route, args=[self.document.id])
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    def test_the_attempt_is_still_audited(self):
        self.client.get(reverse("document-detail", args=[self.document.id]))

        self.assertTrue(
            AuditLog.objects.filter(
                user=self.outsider,
                document=self.document,
                result=AuditLog.Result.DENIED,
            ).exists()
        )


@override_settings(**EAGER)
class VersionDownloadTests(APITestCase):
    def setUp(self):
        self.owner = make_user("download_owner")
        self.client.force_authenticate(self.owner)
        self.document = make_document(self.owner, content=b"first edition")
        with mock.patch.object(services.process_document_task, "delay"):
            services.create_document_version(
                self.document, make_txt_file(content=b"second edition"), self.owner
            )

    def download(self, **params):
        return self.client.get(
            reverse("document-download", args=[self.document.id]), params
        )

    def test_the_current_version_is_the_default(self):
        self.assertEqual(b"".join(self.download().streaming_content), b"second edition")

    def test_an_earlier_version_can_be_fetched_and_is_audited(self):
        response = self.download(version="1")

        self.assertEqual(b"".join(response.streaming_content), b"first edition")
        entry = AuditLog.objects.filter(
            action=AuditLog.Action.DOCUMENT_DOWNLOAD, document=self.document
        ).latest("id")
        self.assertEqual(entry.metadata.get("version_number"), 1)

    def test_a_missing_or_malformed_version_is_refused(self):
        self.assertEqual(
            self.download(version="99").status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(
            self.download(version="one").status_code, status.HTTP_400_BAD_REQUEST
        )


@override_settings(**EAGER)
class StructureEndpointTests(APITestCase):
    def setUp(self):
        self.owner = make_user("structure_owner")
        self.client.force_authenticate(self.owner)
        table = (
            b"FEE STRUCTURE\n\n"
            b"Programme\tTuition\tHostel\n"
            b"B.Tech CSE\t85000\t60000\n"
            b"B.Tech MECH\t80000\t58000\n"
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": "Fees", "file": make_txt_file("fees.txt", table)},
                format="multipart",
            )
        self.document_id = response.data["id"]

    def test_tables_come_from_the_live_pass_with_their_span(self):
        response = self.client.get(reverse("document-tables", args=[self.document_id]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        table = response.data[0]
        self.assertEqual(table["header"], ["Programme", "Tuition", "Hostel"])
        self.assertEqual(table["page_number"], 1)
        self.assertLess(table["character_start"], table["character_end"])

    def test_figures_is_an_empty_list_for_a_text_document(self):
        response = self.client.get(reverse("document-figures", args=[self.document_id]))
        self.assertEqual(response.data, [])

    def test_detail_says_what_the_caller_may_do(self):
        mine = self.client.get(reverse("document-detail", args=[self.document_id]))
        self.assertTrue(mine.data["can_manage"])
        self.assertEqual(mine.data["owner_username"], "structure_owner")

        colleague = make_user("structure_colleague", role=UserProfile.Role.STUDENT)
        client = APIClient()
        client.force_authenticate(colleague)
        theirs = client.get(reverse("document-detail", args=[self.document_id]))
        self.assertFalse(theirs.data["can_manage"])


class SeedE2ETests(TestCase):
    """Test accounts with a known password must never reach a real database."""

    def test_it_refuses_a_database_that_is_not_marked_disposable(self):
        from django.core.management.base import CommandError

        with mock.patch.dict(
            connection.settings_dict, {"NAME": "ai_document_intelligence"}
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "seed_e2e", "--password", "long-enough-password", stdout=StringIO()
                )

        from django.contrib.auth import get_user_model

        self.assertFalse(get_user_model().objects.filter(username="admin").exists())

    def test_it_seeds_a_disposable_one(self):
        """The test database's own name ("test_...") marks it disposable."""
        call_command(
            "seed_e2e", "--password", "long-enough-password", stdout=StringIO()
        )

        from django.contrib.auth import get_user_model

        admin = get_user_model().objects.get(username="admin")
        self.assertEqual(admin.profile.role, UserProfile.Role.SYSTEM_ADMIN)
        self.assertTrue(admin.check_password("long-enough-password"))
