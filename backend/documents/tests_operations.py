"""Audit trail, processing monitor, system status, readiness, dashboard,
conversation management and citation source inspection."""

import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import UserProfile
from documents import services
from documents.models import AuditLog, Conversation, Document, Message, ProcessingJob
from documents.operations import health
from documents.processing.errors import ErrorCode
from documents.tests import MEDIA_ROOT, make_txt_file, make_user

Role = UserProfile.Role
EAGER = {
    "MEDIA_ROOT": MEDIA_ROOT,
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
}


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def queued_document(owner, title="Policy"):
    document = Document.objects.create(
        title=title, owner=owner, department=owner.profile.department
    )
    with mock.patch.object(services.process_document_task, "delay"):
        services.create_document_version(document, make_txt_file(), owner)
    document.refresh_from_db()
    return document


class ScopedTestCase(TestCase):
    def setUp(self):
        self.root = make_user("ops_root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user(
            "ops_phy_admin", Role.DEPARTMENT_ADMIN, "Physics"
        )
        self.physics_member = make_user("ops_phy", Role.FACULTY, "Physics")
        self.chemistry_member = make_user("ops_che", Role.FACULTY, "Chemistry")


@override_settings(**EAGER)
class AuditTrailTests(ScopedTestCase):
    def setUp(self):
        super().setUp()
        self.physics_doc = queued_document(self.physics_member, "Lab Safety")
        self.chemistry_doc = queued_document(self.chemistry_member, "Salary Revisions")

        # A physics member is denied a chemistry document; a chemistry
        # member reads their own.
        AuditLog.objects.create(
            user=self.physics_member,
            action=AuditLog.Action.DOCUMENT_VIEW,
            document=self.chemistry_doc,
            result=AuditLog.Result.DENIED,
        )
        AuditLog.objects.create(
            user=self.chemistry_member,
            action=AuditLog.Action.DOCUMENT_VIEW,
            document=self.chemistry_doc,
            result=AuditLog.Result.ALLOWED,
        )

    def rows(self, user, **params):
        response = client_for(user).get(reverse("admin-audit"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["results"]

    def test_a_department_admin_sees_their_members_and_their_documents(self):
        rows = self.rows(self.physics_admin)
        users = {row["username"] for row in rows}

        self.assertIn("ops_phy", users)
        self.assertNotIn("ops_che", users)

    def test_another_departments_title_is_withheld_from_a_department_admin(self):
        denied = [
            row
            for row in self.rows(self.physics_admin, result="DENIED")
            if row["document"] == self.chemistry_doc.id
        ]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["document_title"], "")

    def test_a_system_admin_sees_everything_including_titles(self):
        rows = self.rows(self.root, document=str(self.chemistry_doc.id))
        self.assertTrue(
            all(row["document_title"] == "Salary Revisions" for row in rows)
        )
        self.assertGreaterEqual(len(rows), 2)

    def test_filters_are_validated(self):
        response = client_for(self.root).get(reverse("admin-audit"), {"user": "x"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(**EAGER)
class ProcessingMonitorTests(ScopedTestCase):
    def setUp(self):
        super().setUp()
        self.physics_doc = queued_document(self.physics_member)
        self.chemistry_doc = queued_document(self.chemistry_member)
        self.physics_job = ProcessingJob.objects.get(
            document_version=self.physics_doc.current_version
        )
        ProcessingJob.objects.filter(pk=self.physics_job.pk).update(
            status=ProcessingJob.Status.FAILED, error_code=ErrorCode.OCR_FAILED
        )

    def test_jobs_are_scoped_to_the_department(self):
        response = client_for(self.physics_admin).get(reverse("admin-jobs"))
        documents = {row["document_id"] for row in response.data["results"]}

        self.assertIn(self.physics_doc.id, documents)
        self.assertNotIn(self.chemistry_doc.id, documents)

    def test_stalled_jobs_are_flagged(self):
        chemistry_job = ProcessingJob.objects.get(
            document_version=self.chemistry_doc.current_version
        )
        ProcessingJob.objects.filter(pk=chemistry_job.pk).update(
            status=ProcessingJob.Status.PROCESSING,
            updated_at=timezone.now() - timedelta(hours=10),
        )
        response = client_for(self.root).get(reverse("admin-jobs"), {"stalled": "true"})

        self.assertEqual(
            [row["id"] for row in response.data["results"]], [chemistry_job.id]
        )
        self.assertTrue(response.data["results"][0]["is_stalled"])

    def test_a_failed_job_can_be_retried_and_the_retry_is_audited(self):
        with mock.patch.object(services.process_document_task, "delay"):
            response = client_for(self.physics_admin).post(
                reverse("admin-job-retry", args=[self.physics_job.id])
            )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotEqual(response.data["id"], self.physics_job.id)
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.JOB_RETRY).exists()
        )

    def test_retrying_while_a_run_is_active_is_a_conflict(self):
        with mock.patch.object(services.process_document_task, "delay"):
            client = client_for(self.root)
            client.post(reverse("admin-job-retry", args=[self.physics_job.id]))
            second = client.post(reverse("admin-job-retry", args=[self.physics_job.id]))
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)

    def test_a_superseded_versions_job_is_not_retried(self):
        with mock.patch.object(services.process_document_task, "delay"):
            services.create_document_version(
                self.physics_doc, make_txt_file(), self.physics_member
            )
            response = client_for(self.root).post(
                reverse("admin-job-retry", args=[self.physics_job.id])
            )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_departments_job_is_not_found(self):
        chemistry_job = ProcessingJob.objects.get(
            document_version=self.chemistry_doc.current_version
        )
        response = client_for(self.physics_admin).post(
            reverse("admin-job-retry", args=[chemistry_job.id])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class SystemStatusTests(ScopedTestCase):
    def test_only_a_system_admin_may_see_it(self):
        self.assertEqual(
            client_for(self.physics_admin).get(reverse("admin-system")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_it_reports_components_without_secrets(self):
        from django.conf import settings

        # The suite has no broker and no media root to probe; those two
        # checks are exercised on their own in ReadinessTests.
        with (
            mock.patch.dict(
                health.CHECKS, {"broker": lambda: None, "storage": lambda: None}
            ),
            mock.patch(
                "documents.operations.health.queues.collect", return_value={"ingest": 0}
            ),
        ):
            response = client_for(self.root).get(reverse("admin-system"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in ("components", "ai", "documents", "jobs_last_24h", "queues"):
            self.assertIn(key, response.data)

        body = json.dumps(response.data, default=str)
        for secret in (settings.SECRET_KEY, settings.DATABASES["default"]["PASSWORD"]):
            if secret:
                self.assertNotIn(secret, body)


class ReadinessTests(TestCase):
    def test_ready_when_every_dependency_answers(self):
        with mock.patch.dict(
            health.CHECKS,
            {"database": lambda: None, "cache": lambda: None, "broker": lambda: None},
        ):
            response = self.client.get("/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_a_failed_dependency_takes_the_instance_out_of_rotation(self):
        def broker_down():
            raise ConnectionError("redis://:hunter2@broker.internal:6379 refused")

        with mock.patch.dict(
            health.CHECKS,
            {"database": lambda: None, "cache": lambda: None, "broker": broker_down},
        ):
            response = self.client.get("/ready/")

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["components"]["broker"]["error"], "ConnectionError")
        # The exception's message - with its host and password - never leaves.
        self.assertNotIn("hunter2", json.dumps(body))
        self.assertNotIn("broker.internal", json.dumps(body))

    def test_the_real_database_and_cache_checks_pass(self):
        self.assertEqual(health._timed(health.check_database)["status"], health.OK)
        self.assertEqual(health._timed(health.check_cache)["status"], health.OK)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_the_storage_check_writes_reads_and_cleans_up(self):
        self.assertEqual(health._timed(health.check_storage)["status"], health.OK)


@override_settings(**EAGER)
class DashboardTests(ScopedTestCase):
    def test_counts_only_what_the_viewer_may_open(self):
        queued_document(self.physics_member, "Physics Doc")
        queued_document(self.chemistry_member, "Chemistry Doc")

        response = client_for(self.physics_member).get(reverse("dashboard"))

        self.assertEqual(response.data["documents"]["total"], 1)
        titles = [row["title"] for row in response.data["recent_documents"]]
        self.assertEqual(titles, ["Physics Doc"])

    def test_needs_attention_lists_only_documents_the_viewer_can_act_on(self):
        mine = queued_document(self.physics_member, "Mine")
        colleague = make_user("ops_phy_2", Role.FACULTY, "Physics")
        theirs = queued_document(colleague, "Theirs")
        Document.objects.filter(pk__in=[mine.pk, theirs.pk]).update(
            status=Document.Status.FAILED
        )

        response = client_for(self.physics_member).get(reverse("dashboard"))
        titles = [row["title"] for row in response.data["needs_attention"]]

        self.assertEqual(titles, ["Mine"])


class ConversationManagementTests(APITestCase):
    def setUp(self):
        self.owner = make_user("conv_owner")
        self.stranger = make_user("conv_stranger")
        self.conversation = Conversation.objects.create(
            owner=self.owner, title="Attendance rules"
        )
        Message.objects.create(
            conversation=self.conversation, role=Message.Role.USER, content="q"
        )
        Conversation.objects.create(owner=self.stranger, title="Someone else's")

    def test_the_list_holds_only_the_callers_own_conversations(self):
        response = client_for(self.owner).get(reverse("conversation-list"))
        rows = response.data["results"]

        self.assertEqual([row["title"] for row in rows], ["Attendance rules"])
        self.assertEqual(rows[0]["message_count"], 1)

    def test_rename_and_delete(self):
        client = client_for(self.owner)
        url = reverse("conversation-detail", args=[self.conversation.id])

        renamed = client.patch(url, {"title": "Exam eligibility"}, format="json")
        self.assertEqual(renamed.data["title"], "Exam eligibility")

        self.assertEqual(client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Conversation.objects.filter(pk=self.conversation.pk).exists())

    def test_another_users_conversation_does_not_exist_for_you(self):
        client = client_for(self.stranger)
        url = reverse("conversation-detail", args=[self.conversation.id])

        self.assertEqual(client.get(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(client.delete(url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation.pk).exists())


@override_settings(**EAGER)
class ChatTitleAndSourceTests(APITestCase):
    PAGE = b"Clause 4.2 Attendance\nA student must maintain a minimum of 75 percent attendance in each course to be eligible for the end-semester examination.\n"

    def setUp(self):
        self.owner = make_user("source_owner")
        self.outsider = make_user("source_outsider", department="Mechanical")
        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("document-list"),
                {"title": "Regulations", "file": make_txt_file(content=self.PAGE)},
                format="multipart",
            )
        self.document_id = response.data["id"]

    def ask(self):
        return self.client.post(
            reverse("chat"),
            {
                "question": "What is the minimum attendance required for the examination?"
            },
            format="json",
        )

    def test_a_new_conversation_is_titled_from_its_first_question(self):
        conversation = Conversation.objects.get(pk=self.ask().data["conversation_id"])
        self.assertTrue(conversation.title.startswith("What is the minimum attendance"))

    def test_a_citation_resolves_to_its_exact_evidence(self):
        answer = self.ask().data
        self.assertFalse(answer["abstained"], answer)
        citation = answer["citations"][0]

        source = self.client.get(
            reverse("document-source", args=[citation["document_id"]]),
            {"chunk": citation["chunk_id"]},
        ).data

        self.assertTrue(source["is_current"])
        span = source["page_text"][source["character_start"] : source["character_end"]]
        self.assertEqual(span, source["chunk_text"])
        self.assertIn("75 percent", span)

    def test_evidence_from_a_superseded_pass_says_so(self):
        citation = self.ask().data["citations"][0]
        document = Document.objects.get(pk=self.document_id)
        with self.captureOnCommitCallbacks(execute=True):
            services.reprocess_document(document)

        source = self.client.get(
            reverse("document-source", args=[self.document_id]),
            {"chunk": citation["chunk_id"]},
        ).data
        self.assertFalse(source["is_current"])

    def test_source_inspection_is_scoped_like_every_other_read(self):
        citation = self.ask().data["citations"][0]
        other = Document.objects.create(title="Other", owner=self.owner, department="")

        wrong_document = self.client.get(
            reverse("document-source", args=[other.id]),
            {"chunk": citation["chunk_id"]},
        )
        outsider = client_for(self.outsider).get(
            reverse("document-source", args=[self.document_id]),
            {"chunk": citation["chunk_id"]},
        )

        self.assertEqual(wrong_document.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(outsider.status_code, status.HTTP_404_NOT_FOUND)
