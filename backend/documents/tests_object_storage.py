"""The pipeline works against a storage with no filesystem paths (§15).

`Storage.path()` raises NotImplementedError on S3 and every other remote
backend. The pipeline read `document_version.file.path` directly, so the
platform could not be pointed at object storage at all - the first stage
raised before any document was looked at.

These tests use a storage that behaves exactly like a remote one, which
proves the seam without needing a bucket, credentials, a network, or
django-storages installed. The point of the abstraction is that the stages
do not know the difference; a fake that refuses `path()` tests precisely
that.
"""

import os

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings

from documents import storage
from documents.models import (
    Document,
    DocumentChunk,
    DocumentContent,
    ProcessingJob,
)
from documents.processing.pipeline import run_pipeline
from documents.tests import MEDIA_ROOT, make_user

BODY = (
    b"ACADEMIC REGULATIONS R20\n"
    b"Clause 4.2 Attendance minimum is 75 percent for examination eligibility.\n"
)


class PathlessStorage(FileSystemStorage):
    """Stores on disk, but refuses to admit a path - like S3.

    Subclassing FileSystemStorage keeps the test honest about everything
    EXCEPT the one behaviour under test: bytes really are written and read
    back through the storage API, and only `path()` is taken away.

    Every inherited method that reaches for `path()` has to be rerouted to
    `_disk`, because FileSystemStorage implements `open`, `exists`, `size`
    and `delete` in terms of it. A remote backend implements all four
    WITHOUT a path - that is the whole difference - so a double that lets
    `open()` fall through to `path()` would fail for a reason S3 never
    would, and prove nothing about the seam.
    """

    opened = 0

    def _disk(self, name):
        return super().path(name)

    def path(self, name):
        raise NotImplementedError("This backend doesn't support absolute paths.")

    def _open(self, name, mode="rb"):
        from django.core.files import File

        PathlessStorage.opened += 1
        return File(open(self._disk(name), mode))

    def exists(self, name):
        return os.path.exists(self._disk(name))

    def size(self, name):
        return os.path.getsize(self._disk(name))

    def delete(self, name):
        if os.path.exists(self._disk(name)):
            os.remove(self._disk(name))

    def _save(self, name, content):
        full = self._disk(name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as destination:
            for chunk in content.chunks():
                destination.write(chunk)
        return name


pathless = PathlessStorage(location=str(MEDIA_ROOT))


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class LocalCopyTests(TestCase):
    def setUp(self):
        self.user = make_user("storage_seam_user", department="Registry")

    def make_version(self, storage_backend=None):
        document = Document.objects.create(
            title="Regulations",
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(BODY, name="r20.txt"),
            file_name="r20.txt",
            file_size=len(BODY),
            uploaded_by=self.user,
        )
        if storage_backend is not None:
            version.file.storage = storage_backend
        document.current_version = version
        document.save(update_fields=["current_version"])
        return document, version

    def test_a_local_storage_is_used_in_place_and_never_copied(self):
        """Copying a file that is already on disk would double the IO of
        every job for nothing."""
        _, version = self.make_version()
        real = version.file.path

        with storage.local_copy(version.file) as path:
            self.assertEqual(path, real)

        self.assertTrue(os.path.exists(real), "the original was deleted")

    def test_a_pathless_storage_is_materialized_to_a_real_file(self):
        _, version = self.make_version(pathless)

        with storage.local_copy(version.file) as path:
            self.assertTrue(os.path.exists(path))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), BODY)
            temporary = path

        self.assertFalse(
            os.path.exists(temporary), "the temporary copy was left behind"
        )

    def test_the_temporary_file_is_removed_even_when_the_body_raises(self):
        """A pipeline that fails mid-OCR must not leave the document in
        /tmp - that is a copy of a confidential file outside every access
        control the platform has."""
        _, version = self.make_version(pathless)
        leaked = {}

        with self.assertRaises(RuntimeError):
            with storage.local_copy(version.file) as path:
                leaked["path"] = path
                raise RuntimeError("stage blew up")

        self.assertFalse(os.path.exists(leaked["path"]))

    def test_the_copy_keeps_the_extension(self):
        """Type detection sniffs magic bytes, but the DOCX check and the
        extension fallback both look at the name."""
        _, version = self.make_version(pathless)

        with storage.local_copy(version.file) as path:
            self.assertTrue(path.endswith(".txt"), path)


# The backend is swapped through settings, not by assigning `.storage` on a
# FieldFile. That distinction is what makes these tests mean anything:
# run_pipeline re-fetches the job from the database, which builds a fresh
# DocumentVersion with the DEFAULT storage - so an instance-level swap is
# silently discarded and the "remote" test runs against local disk and
# passes while proving nothing. It did, until the fetch-count assertion
# caught it.
PATHLESS_STORAGES = {
    "default": {
        "BACKEND": "documents.tests_object_storage.PathlessStorage",
        "OPTIONS": {"location": str(MEDIA_ROOT)},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


@override_settings(MEDIA_ROOT=MEDIA_ROOT, STORAGES=PATHLESS_STORAGES)
class PipelineOnRemoteStorageTests(TestCase):
    """The whole pipeline, end to end, with no filesystem path available."""

    def setUp(self):
        self.user = make_user("remote_storage_user", department="Registry")
        PathlessStorage.opened = 0

    def process_on_remote_storage(self, body=BODY, name="r20.txt"):
        document = Document.objects.create(
            title="Regulations",
            document_type="REGULATION",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name=name),
            file_name=name,
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])

        # Counted from here: the upload above legitimately touches storage.
        PathlessStorage.opened = 0

        job = ProcessingJob.objects.create(document_version=version)
        run_pipeline(job.id)
        return document, version, job

    def test_the_storage_really_is_pathless_in_these_tests(self):
        """Guards the guard: if the override ever stops taking effect, every
        test in this class silently reverts to local disk."""
        from django.core.files.storage import default_storage

        self.assertIsInstance(default_storage._wrapped, PathlessStorage)
        with self.assertRaises(NotImplementedError):
            default_storage.path("anything")

    def test_a_document_processes_with_no_filesystem_path(self):
        """Before the seam existed this raised NotImplementedError in the
        first line of run_pipeline, so every document failed."""
        _, version, job = self.process_on_remote_storage()

        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.COMPLETED)

        content = DocumentContent.objects.get(document_version=version)
        page = content.pages.get(page_number=1)
        self.assertIn("75 percent", page.text)

    def test_the_object_is_fetched_once_for_the_whole_job(self):
        """Nine stages fetching the same object nine times would make every
        document nine network round-trips more expensive."""
        self.process_on_remote_storage()

        self.assertEqual(
            PathlessStorage.opened,
            1,
            f"the stored object was opened {PathlessStorage.opened} times",
        )

    def test_indexing_still_produces_retrievable_chunks(self):
        _, version, _ = self.process_on_remote_storage()

        self.assertTrue(DocumentChunk.objects.filter(document_version=version).exists())

    def test_no_temporary_files_survive_the_job(self):
        import glob
        import tempfile

        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.txt")))
        self.process_on_remote_storage()
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "*.txt")))

        self.assertEqual(after - before, set(), "the job leaked a temporary copy")


class BackendReportingTests(TestCase):
    def test_the_default_backend_is_reported_by_name(self):
        self.assertEqual(storage.describe(), "FileSystemStorage")

    def test_a_filesystem_backend_is_not_reported_as_remote(self):
        self.assertFalse(storage.is_remote())
