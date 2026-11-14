"""manage.py reprocess_documents: which documents it queues, and which it leaves.

Documents are processed for real (so they carry genuine content rows), then
some are marked as produced by the previous pipeline. The command is run with
the Celery broker out of the picture: `reprocess_document` registers its
enqueue on commit, which a TestCase never reaches, so what is asserted is the
queued ProcessingJob rows - exactly what the Reprocess button would leave.
"""

from io import StringIO

from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from documents.models import Document, DocumentContent, ProcessingJob
from documents.processing.pipeline import PROCESSING_VERSION, run_pipeline
from documents.tests import make_user

OLD = "ingestion-v1"


class ReprocessDocumentsTests(TestCase):
    def setUp(self):
        self.user = make_user("reprocess_user", department="Registry")

    def processed(self, title, *, department="Registry", version=PROCESSING_VERSION):
        document = Document.objects.create(
            title=title, document_type="OTHER", owner=self.user, department=department
        )
        name = f"{title.lower().replace(' ', '_')}.txt"
        body = f"{title}\nBody text of the document.\n".encode()
        file_version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name=name),
            file_name=name,
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = file_version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=file_version).id)
        DocumentContent.objects.filter(document_version=file_version).update(
            processing_version=version
        )
        return document

    def run_command(self, *args):
        out = StringIO()
        call_command("reprocess_documents", *args, stdout=out)
        return out.getvalue()

    def queued_for(self, document):
        return ProcessingJob.objects.filter(
            document_version=document.current_version,
            status=ProcessingJob.Status.QUEUED,
        ).count()

    def test_only_documents_from_an_older_pipeline_are_queued(self):
        stale = self.processed("Old extraction", version=OLD)
        current = self.processed("Fresh extraction")

        output = self.run_command()

        self.assertEqual(self.queued_for(stale), 1)
        self.assertEqual(self.queued_for(current), 0)
        self.assertIn("Matching : 1 document(s)", output)
        self.assertIn("Queued 1 document(s)", output)

    def test_all_selects_every_live_document(self):
        stale = self.processed("Old extraction", version=OLD)
        current = self.processed("Fresh extraction")

        self.run_command("--all")

        self.assertEqual(self.queued_for(stale), 1)
        self.assertEqual(self.queued_for(current), 1)

    def test_a_file_type_narrows_the_selection(self):
        text = self.processed("A text file", version=OLD)
        sheet = self.processed("A pretend sheet", version=OLD)
        content = DocumentContent.objects.get(document_version=sheet.current_version)
        content.metadata = {**content.metadata, "file_type": "XLSX"}
        content.save(update_fields=["metadata"])

        self.run_command("--file-type", "XLSX")

        self.assertEqual(self.queued_for(sheet), 1)
        self.assertEqual(self.queued_for(text), 0)

    def test_a_department_narrows_the_selection(self):
        registry = self.processed("Registry file", version=OLD)
        library = self.processed("Library file", department="Library", version=OLD)

        self.run_command("--department", "Library")

        self.assertEqual(self.queued_for(library), 1)
        self.assertEqual(self.queued_for(registry), 0)

    def test_a_limit_queues_in_batches(self):
        first = self.processed("First", version=OLD)
        second = self.processed("Second", version=OLD)
        third = self.processed("Third", version=OLD)

        output = self.run_command("--limit", "2")

        queued = [self.queued_for(document) for document in (first, second, third)]
        self.assertEqual(sum(queued), 2)
        self.assertIn("Matching : 3 document(s)", output)
        self.assertIn("Selected : 2", output)

    def test_a_dry_run_reports_and_queues_nothing(self):
        stale = self.processed("Old extraction", version=OLD)

        output = self.run_command("--dry-run")

        self.assertEqual(self.queued_for(stale), 0)
        self.assertIn("Dry run", output)
        self.assertIn("TXT: 1", output)

    def test_an_archived_document_is_left_alone(self):
        archived = self.processed("Archived", version=OLD)
        archived.archived_at = timezone.now()
        archived.save(update_fields=["archived_at"])

        self.run_command()

        self.assertEqual(self.queued_for(archived), 0)

    def test_a_document_already_being_processed_is_skipped_and_reported(self):
        busy = self.processed("Busy", version=OLD)
        ProcessingJob.objects.create(document_version=busy.current_version)

        output = self.run_command()

        self.assertEqual(self.queued_for(busy), 1)  # only the one already there
        self.assertIn("Skipped 1: already being processed", output)

    def test_a_document_that_never_finished_has_nothing_stale_to_replace(self):
        document = Document.objects.create(
            title="Never processed", document_type="OTHER", owner=self.user
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(b"x", name="x.txt"),
            file_name="x.txt",
            file_size=1,
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])

        output = self.run_command("--all")

        self.assertIn("Matching : 0 document(s)", output)

    def test_a_negative_limit_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command("--limit", "-1")

    def test_new_extractions_are_stamped_with_the_current_pipeline(self):
        document = self.processed("Stamped")

        content = DocumentContent.objects.get(document_version=document.current_version)

        self.assertEqual(content.processing_version, PROCESSING_VERSION)
        self.assertNotEqual(PROCESSING_VERSION, OLD)
