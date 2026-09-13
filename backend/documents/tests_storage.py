"""Stored files are reclaimed when their rows are deleted.

Django deletes rows, never files. Before this, deleting a document removed
every trace of it from the database and left the uploaded bytes on disk
permanently - readable to anyone with filesystem access, invisible to every
listing, and recorded in the audit log as a completed deletion.
"""

import os

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from documents.models import Document, ProcessingJob
from documents.tests import MEDIA_ROOT, make_user

BODY = b"ACADEMIC REGULATIONS R20\nClause 4.2 Attendance minimum is 75 percent.\n"


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class StoredFileDeletionTests(TestCase):
    def setUp(self):
        self.user = make_user("storage_user", department="Registry")

    def make_document(self, title="Regulations"):
        document = Document.objects.create(
            title=title,
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        version = self.add_version(document, 1, "r20.txt")
        return document, version

    def add_version(self, document, number, name):
        version = document.versions.create(
            version_number=number,
            file=ContentFile(BODY, name=name),
            file_name=name,
            file_size=len(BODY),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        return version

    def test_deleting_a_document_removes_the_stored_file(self):
        document, version = self.make_document()
        path = version.file.path
        self.assertTrue(os.path.exists(path))

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()

        self.assertFalse(
            os.path.exists(path),
            "the uploaded bytes survived the deletion of every row that "
            "referenced them",
        )

    def test_deleting_one_version_leaves_the_others_alone(self):
        document, first = self.make_document()
        second = self.add_version(document, 2, "r24.txt")
        first_path, second_path = first.file.path, second.file.path
        self.assertNotEqual(first_path, second_path)

        with self.captureOnCommitCallbacks(execute=True):
            first.delete()

        self.assertFalse(os.path.exists(first_path))
        self.assertTrue(os.path.exists(second_path))

    def test_every_version_of_a_deleted_document_is_reclaimed(self):
        document, first = self.make_document()
        second = self.add_version(document, 2, "r24.txt")
        paths = [first.file.path, second.file.path]

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()

        for path in paths:
            self.assertFalse(os.path.exists(path), path)

    def test_the_file_survives_a_rolled_back_delete(self):
        """post_delete fires INSIDE the transaction. Unlinking there would
        mean a rollback restores the row and leaves the file gone - the one
        outcome worse than an orphan."""
        document, version = self.make_document()
        path = version.file.path

        # Callbacks captured but NOT executed: this is exactly the state a
        # rolled-back transaction leaves behind.
        with self.captureOnCommitCallbacks(execute=False):
            document.delete()

        self.assertTrue(
            os.path.exists(path),
            "the file was unlinked before the deletion was durable",
        )

    def test_an_already_missing_file_does_not_fail_the_delete(self):
        document, version = self.make_document()
        os.remove(version.file.path)

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()

        self.assertFalse(Document.objects.filter(id=document.id).exists())

    def test_a_version_with_no_file_is_not_an_error(self):
        document, version = self.make_document()
        version.file = ""
        version.save(update_fields=["file"])

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()

        self.assertFalse(Document.objects.filter(id=document.id).exists())

    def test_deleting_through_the_api_reclaims_the_file(self):
        """The path that actually matters: a user pressing delete."""
        document, version = self.make_document()
        path = version.file.path
        client = APIClient()
        client.force_authenticate(self.user)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.delete(reverse("document-detail", args=[document.id]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(os.path.exists(path))

    def test_processing_artifacts_do_not_keep_the_file_alive(self):
        """A version referenced by a ProcessingJob must still be reclaimable
        - a FK from the job must not silently pin the bytes."""
        document, version = self.make_document()
        ProcessingJob.objects.create(document_version=version)
        path = version.file.path

        with self.captureOnCommitCallbacks(execute=True):
            document.delete()

        self.assertFalse(os.path.exists(path))
