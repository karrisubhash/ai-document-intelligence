"""The sample library: seed_demo and POST /api/v1/admin/samples/."""

import hashlib
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import Department, UserProfile
from documents import samples
from documents.models import AuditLog, Document, DocumentVersion
from documents.processing.stages.ingestion import EXTENSION_TYPES

User = get_user_model()
Role = UserProfile.Role
URL = "/api/v1/admin/samples/"


def make_user(username, role, department=""):
    user = User.objects.create_user(username=username, password="x-long-enough-pass-1")
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


class ManifestTests(TestCase):
    def test_every_sample_exists_is_admissible_and_typed(self):
        entries = samples.samples()
        self.assertGreaterEqual(len(entries), 15)
        self.assertEqual(len({entry.file for entry in entries}), len(entries))
        types = set(Document.DocumentType.values)
        checksums = set()
        for entry in entries:
            with self.subTest(entry.file):
                self.assertTrue(entry.path.is_file())
                self.assertIn(entry.path.suffix, EXTENSION_TYPES)
                self.assertIn(entry.document_type, types)
                self.assertTrue(entry.title.strip())
                checksums.add(hashlib.sha256(entry.path.read_bytes()).hexdigest())
        # Distinct bytes, or idempotency by checksum would skip one of them.
        self.assertEqual(len(checksums), len(entries))
        kinds = {entry.path.suffix for entry in entries}
        self.assertTrue({".pdf", ".png", ".txt"} <= kinds)

    def test_text_samples_have_unix_line_endings(self):
        """Their checksums must not depend on how a checkout was configured
        (.gitattributes keeps the corpus byte for byte)."""
        for entry in samples.samples():
            if entry.path.suffix == ".txt":
                self.assertNotIn(b"\r\n", entry.path.read_bytes(), entry.file)


class SeedTests(TestCase):
    def setUp(self):
        Department.objects.create(name="Physics")

    def test_seeds_through_the_upload_path_once(self):
        first = samples.seed("Physics", actor=None, via="test")
        total = len(samples.samples())
        self.assertEqual(len(first.queued), total)
        self.assertEqual(first.skipped, [])

        documents = Document.objects.filter(department="Physics")
        self.assertEqual(documents.count(), total)
        owner = User.objects.get(username=samples.LIBRARY_USERNAME)
        self.assertFalse(owner.is_active)
        self.assertFalse(owner.has_usable_password())
        for document in documents:
            version = document.current_version
            self.assertEqual(document.owner, owner)
            self.assertEqual(version.uploaded_by, owner)
            self.assertEqual(len(version.checksum), 64)
            self.assertEqual(version.processing_jobs.count(), 1)
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.DOCUMENT_CREATE).count(),
            total,
        )

        again = samples.seed("Physics", actor=None, via="test")
        self.assertEqual(again.queued, [])
        self.assertEqual(len(again.skipped), total)
        self.assertEqual(Document.objects.filter(department="Physics").count(), total)

    def test_adds_back_only_what_is_missing(self):
        samples.seed("Physics", actor=None, via="test")
        Document.objects.filter(title="Library Rules").delete()
        outcome = samples.seed("Physics", actor=None, via="test")
        self.assertEqual([doc.title for doc in outcome.queued], ["Library Rules"])

    def test_departments_are_seeded_separately(self):
        Department.objects.create(name="Chemistry")
        samples.seed("Physics", actor=None, via="test")
        outcome = samples.seed("Chemistry", actor=None, via="test")
        self.assertEqual(len(outcome.queued), len(samples.samples()))


class SeedDemoCommandTests(TestCase):
    @override_settings(DEMO_DEPARTMENT="Demo")
    def test_creates_the_demo_department_and_is_idempotent(self):
        out = StringIO()
        call_command("seed_demo", stdout=out)
        department = Department.objects.get(name="Demo")
        self.assertTrue(department.is_active)
        total = len(samples.samples())
        self.assertEqual(Document.objects.filter(department="Demo").count(), total)
        self.assertIn(f"Queued {total} sample document(s)", out.getvalue())

        out = StringIO()
        call_command("seed_demo", stdout=out)
        self.assertEqual(Document.objects.filter(department="Demo").count(), total)
        self.assertIn(
            f"Queued 0 sample document(s) for processing in 'Demo'; {total}",
            out.getvalue(),
        )

        loads = AuditLog.objects.filter(action=AuditLog.Action.SAMPLES_LOAD)
        self.assertEqual(loads.count(), 2)
        self.assertEqual(loads.first().metadata["via"], "seed_demo")

    def test_refuses_an_inactive_department(self):
        Department.objects.create(name="Retired", is_active=False)
        with self.assertRaisesMessage(Exception, "inactive"):
            call_command("seed_demo", "--department", "Retired", stdout=StringIO())


class SamplesEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        Department.objects.create(name="Physics")
        Department.objects.create(name="Classics", is_active=False)
        self.admin = make_user("root", Role.SYSTEM_ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_queues_the_library_and_reports_what_it_skipped(self):
        total = len(samples.samples())
        response = self.client.post(URL, {"department": "physics"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.json(), {"queued": total, "skipped": 0})
        # The registry's spelling, whatever was typed.
        self.assertEqual(Document.objects.filter(department="Physics").count(), total)

        again = self.client.post(URL, {"department": "Physics"}, format="json")
        self.assertEqual(again.json(), {"queued": 0, "skipped": total})

        load = AuditLog.objects.filter(action=AuditLog.Action.SAMPLES_LOAD).last()
        self.assertEqual(load.user, self.admin)
        self.assertEqual(load.document_department, "Physics")
        self.assertEqual(load.metadata["queued"], total)
        created = AuditLog.objects.filter(action=AuditLog.Action.DOCUMENT_CREATE)
        self.assertEqual(created.count(), total)
        self.assertTrue(all(row.user_id == self.admin.pk for row in created))

    def test_only_system_administrators(self):
        for username, role, department in [
            ("phy_admin", Role.DEPARTMENT_ADMIN, "Physics"),
            ("asha", Role.STUDENT, "Physics"),
        ]:
            client = APIClient()
            client.force_authenticate(make_user(username, role, department))
            response = client.post(URL, {"department": "Physics"}, format="json")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, username)
        self.assertEqual(
            APIClient().post(URL, {"department": "Physics"}, format="json").status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
        self.assertFalse(DocumentVersion.objects.exists())

    def test_names_a_registered_active_department(self):
        for body in (
            {},
            {"department": ""},
            {"department": "Nowhere"},
            {"department": "Classics"},
        ):
            response = self.client.post(URL, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)
            self.assertIn("department", response.json())
        self.assertFalse(Document.objects.exists())

    def test_throttled(self):
        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"samples": "1/hour"}):
            self.assertEqual(
                self.client.post(
                    URL, {"department": "Physics"}, format="json"
                ).status_code,
                202,
            )
            again = self.client.post(URL, {"department": "Physics"}, format="json")
        self.assertEqual(again.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
