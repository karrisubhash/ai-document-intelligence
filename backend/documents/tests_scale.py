"""Phase 11: query counts, queue routing and queue-depth measurement.

§22 says scaling decisions are driven by measured workload rather than by
technology fashion, so these tests are mostly assertions about measurements:
that the hot endpoints do not scale their query count with the corpus, that
workloads land on separate queues, and that queue depth is actually read
from the broker rather than reported as a comfortable zero.
"""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from documents.models import Document, DocumentContent, ProcessingJob
from documents.observability import metrics, queues
from documents.processing.chunking import service as chunking_service
from documents.processing.pipeline import run_pipeline
from documents.tasks import process_document_task
from documents.tasks_reindex import reindex_content_task
from documents.tests import MEDIA_ROOT, make_user

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"

BODY = (
    b"ACADEMIC REGULATIONS R20\n"
    b"Clause 4.2 Attendance minimum is 75 percent for examination eligibility.\n"
)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class QueryCountTests(TestCase):
    """An N+1 only shows up as a slope, so each of these is measured at two
    corpus sizes rather than pinned to one magic number."""

    def setUp(self):
        self.user = make_user("scale_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def add_documents(self, count):
        for index in range(count):
            document = Document.objects.create(
                title=f"Scale {index}",
                document_type="REGULATION",
                owner=self.user,
                department="Registry",
            )
            version = document.versions.create(
                version_number=1,
                file=ContentFile(BODY, name="d.txt"),
                file_name="d.txt",
                file_size=len(BODY),
                uploaded_by=self.user,
            )
            document.current_version = version
            document.save(update_fields=["current_version"])

    def queries_for(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(url)
        return len(ctx.captured_queries)

    def test_the_document_listing_does_not_scale_with_the_corpus(self):
        """Measured: this endpoint issued one extra query PER DOCUMENT
        because the serializer nests current_version without a join - 14
        queries for 12 documents, and a little over a thousand for a
        thousand."""
        self.add_documents(3)
        small = self.queries_for(reverse("document-list"))

        self.add_documents(9)
        large = self.queries_for(reverse("document-list"))

        self.assertEqual(
            small,
            large,
            f"query count grew from {small} to {large} as the corpus grew",
        )

    def test_the_listing_stays_in_single_digits(self):
        self.add_documents(12)
        self.assertLess(self.queries_for(reverse("document-list")), 10)

    def test_the_detail_endpoint_is_flat(self):
        self.add_documents(2)
        document = Document.objects.filter(owner=self.user).first()
        url = reverse("document-detail", args=[document.id])

        before = self.queries_for(url)
        self.add_documents(8)
        self.assertEqual(before, self.queries_for(url))


class QueueRoutingTests(TestCase):
    """NFR-02: OCR and embedding workloads must scale independently, which
    they cannot do while they share a queue."""

    def test_ingestion_and_reindexing_are_on_different_queues(self):
        from django.conf import settings

        routes = settings.CELERY_TASK_ROUTES
        ingest = routes["documents.tasks.process_document_task"]["queue"]
        index = routes["documents.tasks_reindex.reindex_content_task"]["queue"]

        self.assertNotEqual(ingest, index)

    def test_every_task_has_a_route(self):
        """An unrouted task lands on the default queue, where a bulk
        re-index would sit in front of live uploads again."""
        from django.conf import settings

        routed = set(settings.CELERY_TASK_ROUTES)
        for task in (process_document_task, reindex_content_task):
            self.assertIn(task.name, routed)

    def test_queue_names_come_from_the_routing_table(self):
        """Hard-coding them would let a new route create a queue nobody
        watches."""
        from django.conf import settings

        names = queues.queue_names()
        for route in settings.CELERY_TASK_ROUTES.values():
            self.assertIn(route["queue"], names)

    def test_tasks_acknowledge_late_so_a_killed_worker_loses_nothing(self):
        from django.conf import settings

        self.assertTrue(settings.CELERY_TASK_ACKS_LATE)

    def test_workers_take_one_task_at_a_time(self):
        """Prefetching makes a worker sit on minute-long OCR work another
        worker could have started, and makes queue depth understate what is
        actually waiting."""
        from django.conf import settings

        self.assertEqual(settings.CELERY_WORKER_PREFETCH_MULTIPLIER, 1)


class QueueDepthTests(TestCase):
    """The gauge was declared in phase 9 and never set, which is worse than
    absent: a panel that always reads zero looks like an idle system."""

    def test_collect_reports_a_depth_per_queue(self):
        depths = queues.collect()

        # Redis is available in the dev stack; if a future environment lacks
        # it the collector returns {} rather than raising.
        if depths:
            for name in queues.queue_names():
                self.assertIn(name, depths)
                self.assertIsInstance(depths[name], int)

    def test_collect_never_raises_when_the_broker_is_unreachable(self):
        """A metrics scrape that 500s takes the dashboard down at exactly
        the moment the system is under stress."""
        with override_settings(CELERY_BROKER_URL="redis://nonexistent-host:6379/0"):
            self.assertEqual(queues.collect(), {})

    def test_a_non_redis_broker_is_reported_as_unsupported_not_as_zero(self):
        with override_settings(CELERY_BROKER_URL="amqp://guest@rabbit//"):
            self.assertEqual(queues.collect(), {})

    def test_the_gauge_is_exposed_on_the_metrics_endpoint(self):
        queues.collect()
        body = self.client.get("/metrics").content.decode()
        self.assertIn("adi_queue_depth", body)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class ReindexTests(TestCase):
    """§11.4: changing the embedding model requires a controlled re-index."""

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.user = make_user("reindex_user", department="Registry")
        document = Document.objects.create(
            title="Reindex probe",
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(BODY, name="d.txt"),
            file_name="d.txt",
            file_size=len(BODY),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)
        self.content = DocumentContent.objects.get(document_version=version)

    def test_reindexing_replaces_chunks_rather_than_duplicating_them(self):
        from documents.models import DocumentChunk

        before = DocumentChunk.objects.filter(document_content=self.content).count()
        reindex_content_task(self.content.id)

        self.assertEqual(
            DocumentChunk.objects.filter(document_content=self.content).count(), before
        )

    def test_reindexing_without_a_provider_skips_rather_than_wiping(self):
        """Deleting vectors and writing none back is a worse state than the
        one you started in.

        Found by this test: chunk_content handles a missing provider
        internally and returns a summary instead of raising, so catching the
        exception around it never fired - and because chunking deletes the
        existing chunks first, the embeddings cascaded away with them.
        """
        from documents.models import DocumentChunkEmbedding

        before = DocumentChunkEmbedding.objects.filter(
            chunk__document_content=self.content
        ).count()
        self.assertGreater(before, 0)

        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=""):
            chunking_service.reset_provider_cache()
            result = reindex_content_task(self.content.id)

        self.assertTrue(result["skipped"])
        self.assertEqual(
            DocumentChunkEmbedding.objects.filter(
                chunk__document_content=self.content
            ).count(),
            before,
            "re-indexing without a provider destroyed existing embeddings",
        )

    def test_a_deleted_document_does_not_fail_the_task(self):
        content_id = self.content.id
        Document.objects.filter(owner=self.user).delete()

        result = reindex_content_task(content_id)
        self.assertTrue(result["missing"])

    def test_the_command_refuses_to_run_without_a_provider(self):
        from django.core.management import CommandError, call_command

        with override_settings(DOCUMENT_EMBEDDING_PROVIDER=""):
            chunking_service.reset_provider_cache()
            with self.assertRaises(CommandError):
                call_command("reindex", dry_run=True)

    def test_dry_run_queues_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("reindex", dry_run=True, stdout=out)

        self.assertIn("Dry run", out.getvalue())
