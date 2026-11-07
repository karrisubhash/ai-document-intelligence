"""Processing results reach the people a document belongs to - once.

The transitions are made with the pipeline's own functions
(pipeline._mark_started / _mark_completed / _mark_failed / _mark_retrying,
services.reprocess_document, services.recover_stalled_jobs), so these
tests break if the pipeline ever stops announcing its outcomes through a
save of the job, rather than passing against a simulation of it.
"""

import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import UserProfile
from documents import services
from documents.models import Document, ProcessingJob
from documents.processing import pipeline
from documents.processing.errors import ErrorCode, StageError, TransientError
from documents.processing.pipeline import run_pipeline
from documents.processing.scanning import DocumentInfected
from notifications import processing
from notifications.models import Notification

User = get_user_model()
Role = UserProfile.Role
MEDIA_ROOT = tempfile.mkdtemp(prefix="notifications-tests-")


def make_user(username, role=Role.FACULTY, department="Physics"):
    user = User.objects.create_user(username=username, password="pass1234")
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


def text_file(content=b"Tuition fees are due by the 31st of July."):
    return SimpleUploadedFile("fees.txt", content, content_type="text/plain")


def upload(owner, title="Fee structure", *, uploaded_by=None, content=None):
    """A document with one version and its QUEUED job - nothing run yet."""
    document = Document.objects.create(
        title=title, owner=owner, department=owner.profile.department
    )
    add_version(document, uploaded_by or owner, content=content)
    return document


def add_version(document, uploaded_by, content=None):
    with mock.patch.object(services.process_document_task, "delay"):
        services.create_document_version(
            document, text_file(content or b"Fees are due in July."), uploaded_by
        )
    document.refresh_from_db()
    return document


def job_for(document):
    return ProcessingJob.objects.filter(
        document_version=document.current_version
    ).latest("pk")


def notifications_for(user):
    return list(Notification.objects.filter(recipient=user).order_by("pk"))


@override_settings(MEDIA_ROOT=MEDIA_ROOT, NOTIFICATIONS_COALESCE_MINUTES=10)
class ProcessingTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.owner = make_user("owner")

    def run_job(self, document, error=None, job=None):
        """One attempt at the document's job, ending as the pipeline would."""
        job = job or job_for(document)
        document = job.document_version.document
        with self.captureOnCommitCallbacks(execute=True):
            pipeline._mark_started(job, document)
            if error is None:
                pipeline._mark_completed(job, document)
            else:
                pipeline._mark_failed(job, document, error)
        return job


class OutcomeTests(ProcessingTestCase):
    def test_a_finished_upload_tells_its_owner_it_is_ready(self):
        document = upload(self.owner)

        self.run_job(document)

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.ready")
        self.assertEqual(notification.title, "“Fee structure” is ready")
        self.assertEqual(notification.url, f"/documents/{document.pk}")
        self.assertIn("ask a question", notification.body)
        self.assertIsNone(notification.read_at)

    def test_a_failure_says_why_in_plain_words_and_nothing_internal(self):
        document = upload(self.owner)

        self.run_job(
            document,
            StageError(
                "pdfminer choked on /srv/media/documents/2026/fees.pdf at db-host-1",
                code=ErrorCode.CORRUPT_DOCUMENT,
            ),
        )

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.failed")
        self.assertEqual(
            notification.title,
            "“Fee structure” could not be processed: the file appears to be damaged",
        )
        self.assertEqual(notification.url, f"/documents/{document.pk}?tab=processing")
        for leaked in ("/srv", "db-host", "pdfminer", "CORRUPT_DOCUMENT"):
            self.assertNotIn(leaked, notification.title + notification.body)

    def test_a_failure_not_caused_by_the_file_says_so(self):
        document = upload(self.owner)

        self.run_job(document, StageError("boom", code=ErrorCode.UNEXPECTED_ERROR))

        [notification] = notifications_for(self.owner)
        self.assertTrue(
            notification.title.endswith("something went wrong while reading it")
        )
        self.assertIn("not caused by the file", notification.body)

    def test_a_quarantined_file_is_its_own_kind(self):
        document = upload(self.owner)

        self.run_job(document, DocumentInfected("EICAR-Test-Signature"))

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.quarantined")
        self.assertIn("the security scan flagged the file", notification.title)
        self.assertIn("quarantined", notification.body)
        self.assertNotIn("EICAR", notification.title + notification.body)

    def test_a_long_title_is_quoted_shortened(self):
        document = upload(self.owner, title=("Fee structure " * 18).strip())

        self.run_job(document)

        [notification] = notifications_for(self.owner)
        self.assertLessEqual(len(notification.title), 200)
        self.assertIn("…”", notification.title)


class ExactlyOnceTests(ProcessingTestCase):
    def test_a_retry_the_task_makes_itself_is_not_news(self):
        document = upload(self.owner)
        job = job_for(document)

        with self.captureOnCommitCallbacks(execute=True):
            pipeline._mark_started(job, document)
            pipeline._mark_retrying(job, document, TransientError("db blip"))
        self.assertEqual(notifications_for(self.owner), [])

        self.run_job(document, job=job)

        self.assertEqual(len(notifications_for(self.owner)), 1)

    def test_a_redelivered_failed_job_is_not_announced_twice(self):
        document = upload(self.owner)
        job = self.run_job(document, StageError("x", code=ErrorCode.OCR_FAILED))
        [first] = notifications_for(self.owner)
        Notification.objects.filter(pk=first.pk).update(read_at=timezone.now())

        # The broker delivers the same message again and it fails the same way.
        self.run_job(document, StageError("x", code=ErrorCode.OCR_FAILED), job=job)

        self.assertEqual(notifications_for(self.owner), [first])

    def test_a_second_save_of_a_finished_job_is_not_announced_again(self):
        document = upload(self.owner)
        job = self.run_job(document)

        with self.captureOnCommitCallbacks(execute=True):
            job.save()
            job.save(update_fields=["status", "updated_at"])

        self.assertEqual(len(notifications_for(self.owner)), 1)
        self.assertEqual(processing.announce(job.pk), 0)

    def test_a_failed_attempt_that_later_succeeds_reads_as_ready(self):
        document = upload(self.owner)
        job = self.run_job(document, StageError("x", code=ErrorCode.UNEXPECTED_ERROR))

        self.run_job(document, job=job)

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.ready")
        self.assertEqual(notification.title, "“Fee structure” is ready")

    def test_reprocessing_an_unread_result_updates_it_instead_of_adding_one(self):
        document = upload(self.owner)
        self.run_job(document)
        with mock.patch.object(services.process_document_task, "delay"):
            services.reprocess_document(document)

        self.run_job(document)

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.title, "“Fee structure” is ready")
        self.assertEqual(len(notification.payload["documents"]), 1)

    def test_reprocessing_after_it_was_read_is_new_news_once(self):
        document = upload(self.owner)
        self.run_job(document)
        Notification.objects.update(read_at=timezone.now())
        with mock.patch.object(services.process_document_task, "delay"):
            services.reprocess_document(document)

        self.run_job(document)

        self.assertEqual(len(notifications_for(self.owner)), 2)


class SpeaksForTheDocumentTests(ProcessingTestCase):
    def test_a_superseded_versions_result_is_not_news(self):
        document = upload(self.owner)
        first_job = job_for(document)
        add_version(document, self.owner, content=b"Version two.")

        self.run_job(document, job=first_job)

        self.assertEqual(notifications_for(self.owner), [])

        self.run_job(document)
        self.assertEqual(len(notifications_for(self.owner)), 1)

    def test_a_stalled_run_that_reprocess_replaced_is_not_a_failure(self):
        document = upload(self.owner)
        job = job_for(document)
        pipeline._mark_started(job, document)
        ProcessingJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(days=1)
        )

        with (
            mock.patch.object(services.process_document_task, "delay"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            services.reprocess_document(document)

        job.refresh_from_db()
        self.assertEqual(job.error_code, ErrorCode.STALLED)
        self.assertEqual(notifications_for(self.owner), [])

    def test_a_stalled_run_left_failed_is_reported(self):
        document = upload(self.owner)
        job = job_for(document)
        pipeline._mark_started(job, document)
        ProcessingJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(days=1)
        )

        with self.captureOnCommitCallbacks(execute=True):
            services.recover_stalled_jobs()

        [notification] = notifications_for(self.owner)
        self.assertEqual(
            notification.title,
            "“Fee structure” could not be processed: processing was interrupted",
        )

    def test_an_archived_document_is_not_news(self):
        document = upload(self.owner)
        services.archive_document(document, self.owner)

        self.run_job(document)

        self.assertEqual(notifications_for(self.owner), [])


class RecipientTests(ProcessingTestCase):
    def test_the_uploader_of_a_new_version_is_told_too(self):
        admin = make_user("phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        document = upload(self.owner)
        add_version(document, admin, content=b"Revised fees.")

        self.run_job(document)

        self.assertEqual(len(notifications_for(self.owner)), 1)
        self.assertEqual(len(notifications_for(admin)), 1)

    def test_one_person_who_owns_and_uploaded_is_told_once(self):
        document = upload(self.owner)

        self.run_job(document)

        self.assertEqual(Notification.objects.count(), 1)

    def test_nobody_is_told_about_a_document_they_can_no_longer_read(self):
        uploader = make_user("mover", Role.FACULTY, "Physics")
        document = upload(self.owner, uploaded_by=uploader)
        uploader.profile.department = "Chemistry"
        uploader.profile.save()

        self.run_job(document)

        self.assertEqual(notifications_for(uploader), [])
        self.assertEqual(len(notifications_for(self.owner)), 1)

    def test_an_inactive_account_is_not_told(self):
        document = upload(self.owner)
        self.owner.is_active = False
        self.owner.save()

        self.run_job(document)

        self.assertEqual(Notification.objects.count(), 0)

    def test_a_document_without_an_owner_or_uploader_tells_nobody(self):
        document = upload(self.owner)
        Document.objects.filter(pk=document.pk).update(owner=None)
        document.current_version.uploaded_by = None
        document.current_version.save(update_fields=["uploaded_by"])

        self.run_job(document)

        self.assertEqual(Notification.objects.count(), 0)


class CoalescingTests(ProcessingTestCase):
    def test_a_batch_is_one_notification(self):
        documents = [upload(self.owner, title) for title in ("Fees", "Exams", "Hostel")]

        for document in documents:
            self.run_job(document)

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.ready")
        self.assertEqual(notification.title, "3 documents are ready")
        self.assertEqual(notification.body, "Ready: Fees, Exams and Hostel.")
        self.assertEqual(notification.url, "/documents")

    def test_a_mixed_batch_says_both(self):
        fees, exams, hostel = (
            upload(self.owner, title) for title in ("Fees", "Exams", "Hostel")
        )

        self.run_job(fees)
        self.run_job(exams, StageError("x", code=ErrorCode.EMPTY_EXTRACTION))
        self.run_job(hostel)

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.kind, "document.failed")
        self.assertEqual(
            notification.title, "2 documents are ready, 1 could not be processed"
        )
        self.assertEqual(
            notification.body,
            "Ready: Fees and Hostel.\n"
            "Could not be processed: Exams (no readable text was found in it).",
        )

    def test_a_batch_that_all_failed_links_to_the_failed_documents(self):
        for title in ("Fees", "Exams"):
            self.run_job(
                upload(self.owner, title),
                StageError("x", code=ErrorCode.UNSUPPORTED_FILE_TYPE),
            )

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.title, "2 documents could not be processed")
        self.assertEqual(notification.url, "/documents?status=FAILED")

    def test_one_ready_one_failed_reads_naturally(self):
        self.run_job(upload(self.owner, "Fees"))
        self.run_job(
            upload(self.owner, "Exams"), StageError("x", code=ErrorCode.TIMEOUT)
        )

        [notification] = notifications_for(self.owner)
        self.assertEqual(
            notification.title, "1 document is ready, 1 could not be processed"
        )

    def test_many_are_named_briefly(self):
        for index in range(5):
            self.run_job(upload(self.owner, f"Doc {index}"))

        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.body, "Ready: Doc 0, Doc 1, Doc 2 and 2 more.")

    def test_results_after_the_window_start_a_new_notification(self):
        self.run_job(upload(self.owner, "Fees"))
        Notification.objects.update(updated_at=timezone.now() - timedelta(minutes=11))

        self.run_job(upload(self.owner, "Exams"))

        titles = [n.title for n in notifications_for(self.owner)]
        self.assertEqual(titles, ["“Fees” is ready", "“Exams” is ready"])

    def test_results_after_reading_start_a_new_notification(self):
        self.run_job(upload(self.owner, "Fees"))
        Notification.objects.update(read_at=timezone.now())

        self.run_job(upload(self.owner, "Exams"))

        self.assertEqual(len(notifications_for(self.owner)), 2)

    @override_settings(NOTIFICATIONS_COALESCE_MINUTES=0)
    def test_coalescing_can_be_turned_off(self):
        self.run_job(upload(self.owner, "Fees"))
        self.run_job(upload(self.owner, "Exams"))

        self.assertEqual(len(notifications_for(self.owner)), 2)

    def test_a_full_notification_hands_over_to_a_new_one(self):
        with mock.patch.object(processing, "MAX_DOCUMENTS", 2):
            for title in ("Fees", "Exams", "Hostel"):
                self.run_job(upload(self.owner, title))

        titles = [n.title for n in notifications_for(self.owner)]
        self.assertEqual(titles, ["2 documents are ready", "“Hostel” is ready"])


class RobustnessTests(ProcessingTestCase):
    def test_a_notification_that_cannot_be_written_does_not_fail_the_run(self):
        document = upload(self.owner)

        with (
            mock.patch(
                "notifications.service.coalesce", side_effect=RuntimeError("db gone")
            ),
            self.assertLogs("adi.notifications", "ERROR"),
        ):
            job = self.run_job(document)

        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.COMPLETED)
        self.assertEqual(document.status, Document.Status.COMPLETED)
        self.assertEqual(Notification.objects.count(), 0)

    def test_the_real_pipeline_announces_its_result(self):
        document = upload(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            run_pipeline(job_for(document).pk)

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.COMPLETED)
        [notification] = notifications_for(self.owner)
        self.assertEqual(notification.title, "“Fee structure” is ready")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class AutocommitTests(TransactionTestCase):
    """Outside a test transaction, as in a Celery worker: _mark_failed saves
    the job in autocommit, so the announcement runs BEFORE the document's
    status is written. It must not depend on that status."""

    def test_a_failure_in_a_worker_is_announced(self):
        owner = make_user("worker_owner")
        document = Document.objects.create(
            title="Binary blob", owner=owner, department="Physics"
        )
        with mock.patch.object(services.process_document_task, "delay"):
            services.create_document_version(
                document,
                SimpleUploadedFile("blob.bin", b"\x00\x01 not a document"),
                owner,
            )
        job = job_for(document)

        with self.assertRaises(StageError):
            run_pipeline(job.pk)

        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(document.status, Document.Status.FAILED)
        [notification] = Notification.objects.filter(recipient=owner)
        self.assertEqual(
            notification.title,
            "“Binary blob” could not be processed: this type of file is not supported",
        )
