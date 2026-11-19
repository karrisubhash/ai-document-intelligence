"""A platform with no page ceiling: limits that follow the document's length.

See documents/processing/limits.py for why, with the measurements.
"""

import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from config.celery import app
from documents import tasks
from documents.models import Document, DocumentChunk, ProcessingJob
from documents.processing import heartbeat, limits
from documents.processing.chunking import service as chunking_service
from documents.services import create_document_version
from documents.tests import MEDIA_ROOT, make_user
from documents.tests_ingestion import (
    IngestionTestCase,
    make_pdf_bytes,
    make_scanned_pdf_bytes,
)

SMALL_LIMITS = {
    "CELERY_TASK_SOFT_TIME_LIMIT": 100,
    "CELERY_TASK_TIME_LIMIT": 160,
    "INGEST_SECONDS_PER_PAGE": 10,
    "INGEST_MAX_TASK_SECONDS": 3600,
}


@override_settings(**SMALL_LIMITS)
class TimeLimitTests(SimpleTestCase):
    def test_a_document_of_unknown_length_gets_the_base_limit(self):
        self.assertEqual(limits.soft_limit_for(None), 100)
        self.assertEqual(limits.soft_limit_for(0), 100)

    def test_the_limit_grows_with_the_pages(self):
        self.assertEqual(limits.soft_limit_for(1), 110)
        self.assertEqual(limits.soft_limit_for(30), 400)

    def test_the_limit_stops_at_the_ceiling_less_the_grace(self):
        self.assertEqual(limits.soft_limit_for(10**6), 3600 - 60)

    @override_settings(INGEST_MAX_TASK_SECONDS=50)
    def test_the_limit_is_never_below_the_base_whatever_the_ceiling(self):
        self.assertEqual(limits.soft_limit_for(10**6), 100)

    def test_the_hard_limit_keeps_the_base_gap_above_the_soft_one(self):
        self.assertEqual(limits.hard_limit_for(400), 460)

    @override_settings(CELERY_TASK_TIME_LIMIT=100)
    def test_a_hard_limit_set_level_with_the_soft_one_still_gets_a_grace(self):
        self.assertEqual(limits.hard_limit_for(400), 460)

    def test_the_default_allows_a_600_page_pdf_the_time_it_measured_to_take(self):
        # 993 s measured on one CPU; the defaults, not SMALL_LIMITS.
        with override_settings(
            CELERY_TASK_SOFT_TIME_LIMIT=900,
            CELERY_TASK_TIME_LIMIT=960,
            INGEST_SECONDS_PER_PAGE=5,
            INGEST_MAX_TASK_SECONDS=6 * 3600,
        ):
            self.assertGreater(limits.soft_limit_for(600), 3 * 993)


class PdfPageCountTests(SimpleTestCase):
    def count(self, content):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(content)
            return limits.pdf_page_count(str(path))

    def test_counts_the_pages_of_a_pdf(self):
        self.assertEqual(self.count(make_pdf_bytes(["a"] * 7)), 7)

    def test_anything_that_is_not_a_pdf_has_no_count(self):
        self.assertIsNone(self.count(b"just some text"))
        self.assertIsNone(self.count(b"PK\x03\x04 a zip"))

    def test_a_file_that_is_not_a_pdf_is_never_handed_to_the_pdf_parser(self):
        # Up to 50 MB of something else would otherwise be read in full, in
        # the worker, only to be refused - before any real work has begun.
        with mock.patch("documents.processing.extractors.pdf.page_count") as parse:
            self.assertIsNone(self.count(b"PK\x03\x04 a zip"))
            self.assertIsNone(self.count(b"just some text"))

        parse.assert_not_called()

    def test_a_broken_pdf_has_no_count_and_does_not_raise(self):
        self.assertIsNone(self.count(b"%PDF-1.4\nthis is not really a pdf"))

    def test_a_missing_file_has_no_count_and_does_not_raise(self):
        self.assertIsNone(limits.pdf_page_count("/no/such/file.pdf"))


@override_settings(MEDIA_ROOT=MEDIA_ROOT, **SMALL_LIMITS)
class RequeueWithLongerLimitsTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def job_for(self, content, name="long.pdf"):
        user = make_user("limits_user")
        document = Document.objects.create(
            title="Long", owner=user, department="Computer Science"
        )
        with mock.patch("documents.services.enqueue"):
            with self.captureOnCommitCallbacks(execute=True):
                version = create_document_version(
                    document, SimpleUploadedFile(name, content), uploaded_by=user
                )
        return ProcessingJob.objects.get(document_version=version)

    def task(self, *, eager=False, direct=False, timelimit=None):
        task = mock.Mock()
        task.request = SimpleNamespace(
            called_directly=direct, is_eager=eager, timelimit=timelimit
        )
        return task

    def test_a_long_pdf_is_sent_again_with_limits_that_fit_it(self):
        job = self.job_for(make_pdf_bytes(["page"] * 30))
        task = self.task()

        sent = tasks.requeue_with_longer_limits(task, job.id)

        self.assertTrue(sent)
        task.apply_async.assert_called_once_with(
            (job.id,), soft_time_limit=400, time_limit=460
        )

    def test_a_pdf_that_fits_the_base_limits_keeps_them(self):
        # With no per-page allowance the limit is the base for any length.
        job = self.job_for(make_pdf_bytes(["page"] * 3))
        task = self.task()

        with override_settings(INGEST_SECONDS_PER_PAGE=0):
            sent = tasks.requeue_with_longer_limits(task, job.id)

        self.assertFalse(sent)
        task.apply_async.assert_not_called()

    def test_a_message_that_already_carries_the_limits_is_not_sent_again(self):
        job = self.job_for(make_pdf_bytes(["page"] * 30))
        task = self.task(timelimit=(460, 400))

        self.assertFalse(tasks.requeue_with_longer_limits(task, job.id))
        task.apply_async.assert_not_called()

    def test_a_message_with_more_than_needed_is_not_sent_again(self):
        job = self.job_for(make_pdf_bytes(["page"] * 30))
        task = self.task(timelimit=(9999, 9000))

        self.assertFalse(tasks.requeue_with_longer_limits(task, job.id))

    def test_a_message_with_only_the_base_limits_is_sent_again(self):
        # Celery reports (None, None) when the message set no limits.
        job = self.job_for(make_pdf_bytes(["page"] * 30))
        task = self.task(timelimit=(None, None))

        self.assertTrue(tasks.requeue_with_longer_limits(task, job.id))

    def test_a_file_that_is_not_a_pdf_keeps_the_base_limits(self):
        job = self.job_for(b"plain text " * 100, name="notes.txt")
        task = self.task()

        self.assertFalse(tasks.requeue_with_longer_limits(task, job.id))
        task.apply_async.assert_not_called()

    def test_nothing_is_sent_when_run_directly_or_eagerly(self):
        job = self.job_for(make_pdf_bytes(["page"] * 30))

        for kwargs in ({"direct": True}, {"eager": True}):
            task = self.task(**kwargs)
            self.assertFalse(tasks.requeue_with_longer_limits(task, job.id))
            task.apply_async.assert_not_called()

    def test_a_broker_that_refuses_the_message_leaves_the_job_to_run(self):
        job = self.job_for(make_pdf_bytes(["page"] * 30))
        task = self.task()
        task.apply_async.side_effect = ConnectionError("broker down")

        # Falls through to run under the base limits, without raising.
        self.assertFalse(tasks.requeue_with_longer_limits(task, job.id))

    def test_an_unknown_job_leaves_nothing_to_requeue_and_does_not_raise(self):
        task = self.task()

        self.assertFalse(tasks.requeue_with_longer_limits(task, 10**9))

    def test_the_task_does_no_work_when_it_sent_itself_again(self):
        with (
            mock.patch.object(tasks, "requeue_with_longer_limits", return_value=True),
            mock.patch.object(tasks, "run_pipeline") as run,
        ):
            tasks.process_document_task.run(1)

        run.assert_not_called()

    def test_the_task_runs_the_pipeline_when_it_did_not(self):
        with (
            mock.patch.object(tasks, "requeue_with_longer_limits", return_value=False),
            mock.patch.object(tasks, "run_pipeline") as run,
        ):
            tasks.process_document_task.run(1)

        run.assert_called_once()


class HeartbeatTests(TestCase):
    def make_job(self, age_minutes=300):
        user = make_user("beat_user")
        document = Document.objects.create(
            title="Beat", owner=user, department="Computer Science"
        )
        with mock.patch("documents.services.enqueue"):
            with self.captureOnCommitCallbacks(execute=True):
                with override_settings(MEDIA_ROOT=MEDIA_ROOT):
                    version = create_document_version(
                        document, SimpleUploadedFile("a.txt", b"x"), uploaded_by=user
                    )
        job = ProcessingJob.objects.get(document_version=version)
        ProcessingJob.objects.filter(pk=job.pk).update(
            status=ProcessingJob.Status.PROCESSING,
            updated_at=timezone.now() - timedelta(minutes=age_minutes),
        )
        self.addCleanup(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)
        return ProcessingJob.objects.get(pk=job.pk)

    def test_a_beat_makes_a_job_that_looked_dead_look_alive(self):
        from documents.services import stalled_cutoff

        job = self.make_job(age_minutes=300)
        self.assertLess(job.updated_at, stalled_cutoff())

        self.assertTrue(heartbeat.beat(job))

        job.refresh_from_db()
        self.assertGreater(job.updated_at, stalled_cutoff())

    def test_a_beating_job_is_not_recovered_as_stalled(self):
        from documents.services import recover_stalled_jobs

        job = self.make_job(age_minutes=300)
        heartbeat.beat(job)

        self.assertEqual(recover_stalled_jobs(), [])
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.PROCESSING)

    def test_beats_are_spaced(self):
        job = self.make_job()

        self.assertTrue(heartbeat.beat(job, every=60))
        self.assertFalse(heartbeat.beat(job, every=60))
        self.assertFalse(heartbeat.beat(job, every=60))

    def test_a_beat_is_due_again_once_the_interval_has_passed(self):
        job = self.make_job()
        heartbeat.beat(job, every=60)
        job._last_heartbeat -= 61

        self.assertTrue(heartbeat.beat(job, every=60))

    def test_no_job_means_nothing_to_beat(self):
        self.assertFalse(heartbeat.beat(None))

    def test_a_database_error_is_swallowed(self):
        job = self.make_job()

        with mock.patch.object(
            ProcessingJob.objects, "filter", side_effect=RuntimeError("db gone")
        ):
            self.assertFalse(heartbeat.beat(job))


class EmbeddingProgressTests(SimpleTestCase):
    """embed_chunks reports before every batch, and only if asked to."""

    def run_embed(self, count, on_batch):
        # Unsaved rows: the model wants real instances, and bulk_create is
        # patched below so nothing is written.
        chunks = [DocumentChunk(text=f"chunk {i}") for i in range(count)]
        provider = mock.Mock(name="provider", version="1", semantic=False)
        provider.name = "fake"
        provider.embed.side_effect = lambda texts: SimpleNamespace(
            vectors=[[0.0]] * len(texts),
            model_name="fake",
            model_version="1",
            dimensions=1,
            semantic=False,
            metadata={},
        )
        with mock.patch.object(
            chunking_service.DocumentChunkEmbedding.objects, "bulk_create"
        ):
            return chunking_service.embed_chunks(chunks, provider, on_batch=on_batch)

    def test_reports_once_per_batch(self):
        calls = []
        size = chunking_service.EMBED_BATCH_SIZE

        result = self.run_embed(size * 2 + 1, lambda: calls.append(1))

        self.assertEqual(len(calls), 3)
        self.assertEqual(result["embedded"], size * 2 + 1)

    def test_works_without_a_callback_as_the_reindex_calls_it(self):
        self.assertEqual(self.run_embed(5, None)["embedded"], 5)


@override_settings(
    DOCUMENT_EMBEDDING_PROVIDER=(
        "documents.processing.embedding.hashing.HashingEmbeddingProvider"
    )
)
class HeartbeatWiringTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

    def test_indexing_beats_for_the_job_it_belongs_to(self):
        with mock.patch("documents.processing.chunking.service.heartbeat.beat") as beat:
            response = self.upload(
                "a.pdf", make_pdf_bytes(["Attendance rules"]), "application/pdf"
            )

        job = self.job_for(response.data["id"])
        self.assertEqual(job.status, ProcessingJob.Status.COMPLETED)
        # One chunk, so one batch - and a text PDF needs no OCR, so every
        # beat here is indexing's.
        self.assertEqual(beat.call_count, 1)
        self.assertEqual(beat.call_args.args[0].pk, job.pk)


# No embedding provider: the only beats are the OCR stage's own. (With one
# configured, indexing beats too and an OCR stage that never did would pass.)
@override_settings(DOCUMENT_EMBEDDING_PROVIDER="")
class OcrHeartbeatWiringTests(IngestionTestCase):
    def setUp(self):
        super().setUp()
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

    def test_ocr_beats_once_per_page_for_the_job_it_belongs_to(self):
        with mock.patch("documents.processing.stages.ocr.heartbeat.beat") as beat:
            response = self.upload(
                "scan.pdf", make_scanned_pdf_bytes(), "application/pdf"
            )

        job = self.job_for(response.data["id"])
        self.assertEqual(job.status, ProcessingJob.Status.COMPLETED)
        self.assertEqual(beat.call_count, 1)  # a one-page scan
        self.assertEqual(beat.call_args.args[0].pk, job.pk)


class BrokerSettingsTests(SimpleTestCase):
    def test_redis_does_not_redeliver_a_task_before_its_longest_limit(self):
        # With acks_late a running task is unacknowledged, and a visibility
        # timeout shorter than the task starts it a second time beside itself.
        timeout = app.conf.broker_transport_options["visibility_timeout"]

        self.assertGreater(timeout, settings.INGEST_MAX_TASK_SECONDS)
        self.assertGreater(timeout, settings.CELERY_TASK_TIME_LIMIT)

    def test_there_is_no_page_ceiling_by_default(self):
        self.assertEqual(settings.MAX_DOCUMENT_PAGES, 0)
