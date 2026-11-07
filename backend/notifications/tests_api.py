"""The notifications API and the administrators' attention counts."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from accounts.models import SignupRequest, UserProfile
from documents.models import AuditLog, Document, DocumentVersion, ProcessingJob
from notifications import attention, service
from notifications.models import Notification

User = get_user_model()
Role = UserProfile.Role
FIELDS = {"id", "kind", "title", "body", "url", "created_at", "updated_at", "read_at"}


def make_user(username, role=Role.STUDENT, department="Physics"):
    user = User.objects.create_user(username=username, password="pass1234")
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


class NotificationApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.nia = make_user("nia")
        self.sam = make_user("sam")
        self.client = client_for(self.nia)

    def note(self, user=None, title="News", minutes_ago=0, **fields):
        notification = service.notify(user or self.nia, "system.note", title, **fields)
        if minutes_ago:
            stamp = timezone.now() - timedelta(minutes=minutes_ago)
            Notification.objects.filter(pk=notification.pk).update(
                created_at=stamp, updated_at=stamp
            )
            notification.refresh_from_db()
        return notification

    def test_the_list_is_yours_newest_first_without_bookkeeping(self):
        older = self.note(title="Older", minutes_ago=5, payload={"secret": 1})
        newer = self.note(title="Newer", url="/documents/4")
        self.note(self.sam, title="Sam's")

        response = self.client.get(reverse("notification-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [row["id"] for row in response.data["results"]], [newer.pk, older.pk]
        )
        row = response.data["results"][0]
        self.assertEqual(set(row), FIELDS)
        self.assertEqual(row["url"], "/documents/4")
        self.assertIsNone(row["read_at"])
        self.assertEqual(set(response.data), {"count", "next", "previous", "results"})

    def test_unread_only(self):
        read = self.note(title="Read")
        service.mark_read(self.nia, read.pk)
        unread = self.note(title="Unread")

        response = self.client.get(reverse("notification-list"), {"unread": "1"})

        self.assertEqual([row["id"] for row in response.data["results"]], [unread.pk])

    def test_pages_of_twenty_and_a_bounded_page_size(self):
        for index in range(25):
            self.note(title=f"News {index}")

        first = self.client.get(reverse("notification-list"))
        self.assertEqual((first.data["count"], len(first.data["results"])), (25, 20))
        self.assertIsNotNone(first.data["next"])
        second = self.client.get(reverse("notification-list"), {"page": 2})
        self.assertEqual(len(second.data["results"]), 5)
        small = self.client.get(reverse("notification-list"), {"page_size": 5})
        self.assertEqual(len(small.data["results"]), 5)
        huge = self.client.get(reverse("notification-list"), {"page_size": 500})
        self.assertEqual(len(huge.data["results"]), 25)

    def test_summary_counts_unread_and_says_when_news_last_came(self):
        empty = self.client.get(reverse("notification-summary"))
        self.assertEqual(empty.data, {"unread": 0, "latest_at": None})

        read = self.note(title="Read", minutes_ago=10)
        service.mark_read(self.nia, read.pk)
        latest = self.note(title="Unread")
        self.note(self.sam, title="Sam's")

        response = self.client.get(reverse("notification-summary"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["unread"], 1)
        self.assertEqual(response.data["latest_at"], latest.updated_at)

    def test_marking_one_read(self):
        mine = self.note()

        response = self.client.post(reverse("notification-read", args=[mine.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        mine.refresh_from_db()
        self.assertIsNotNone(mine.read_at)

    def test_someone_elses_notification_is_not_found(self):
        theirs = self.note(self.sam, title="Sam's")

        response = self.client.post(reverse("notification-read", args=[theirs.pk]))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.read_at)
        missing = self.client.post(reverse("notification-read", args=[theirs.pk + 99]))
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(missing.data, response.data)

    def test_marking_all_read(self):
        self.note(title="One")
        self.note(title="Two")
        self.note(self.sam, title="Sam's")

        response = self.client.post(reverse("notification-read-all"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {"updated": 2})
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.sam, read_at__isnull=True
            ).exists()
        )

    def test_marking_all_read_up_to_what_was_seen(self):
        seen = self.note(title="Seen", minutes_ago=5)
        later = self.note(title="Arrived after the person looked")

        response = self.client.post(
            reverse("notification-read-all"),
            {"until": seen.updated_at.isoformat()},
            format="json",
        )

        self.assertEqual(response.data, {"updated": 1})
        later.refresh_from_db()
        self.assertIsNone(later.read_at)

    def test_a_bad_until_is_a_400(self):
        for until in ("yesterday", "2026-13-45T00:00:00Z", 12):
            with self.subTest(until=until):
                response = self.client.post(
                    reverse("notification-read-all"), {"until": until}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("until", response.data)

    def test_signed_out_gets_nothing(self):
        self.note()
        anonymous = APIClient()
        for response in (
            anonymous.get(reverse("notification-list")),
            anonymous.get(reverse("notification-summary")),
            anonymous.post(reverse("notification-read-all")),
        ):
            self.assertIn(
                response.status_code,
                (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
            )

    def test_served_under_both_prefixes(self):
        self.note()
        for prefix in ("/api/v1", "/api"):
            response = self.client.get(f"{prefix}/notifications/summary/")
            self.assertEqual(response.data["unread"], 1, prefix)

    def test_reading_writes_no_audit_rows(self):
        mine = self.note()
        before = AuditLog.objects.count()

        self.client.get(reverse("notification-list"))
        self.client.get(reverse("notification-summary"))
        self.client.post(reverse("notification-read", args=[mine.pk]))
        self.client.post(reverse("notification-read-all"))

        self.assertEqual(AuditLog.objects.count(), before)

    def test_polling_has_its_own_limit(self):
        with mock.patch.dict(
            ScopedRateThrottle.THROTTLE_RATES, {"notifications": "2/minute"}
        ):
            codes = [
                self.client.get(reverse("notification-summary")).status_code
                for _ in range(3)
            ]

        self.assertEqual(codes, [200, 200, 429])


class AttentionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.system_admin = make_user("root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user("phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        self.student = make_user("student", Role.STUDENT, "Physics")

    def document(self, department, status_, archived=False):
        owner = make_user(f"owner{Document.objects.count()}", department=department)
        return Document.objects.create(
            title="Doc",
            owner=owner,
            department=department,
            status=status_,
            archived_at=timezone.now() if archived else None,
        )

    def stalled_job(self, department, minutes=24 * 60):
        document = self.document(department, Document.Status.PROCESSING)
        version = DocumentVersion.objects.create(
            document=document,
            version_number=1,
            file="documents/x.txt",
            file_name="x.txt",
            file_size=1,
        )
        job = ProcessingJob.objects.create(
            document_version=version, status=ProcessingJob.Status.PROCESSING
        )
        ProcessingJob.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(minutes=minutes)
        )
        return job

    def pending(self, username, department):
        return SignupRequest.objects.create(
            username=username,
            email=f"{username}@example.edu",
            first_name=username,
            department=department,
        )

    def counts(self, user):
        response = client_for(user).get(reverse("admin-attention"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def populate(self):
        self.document("Physics", Document.Status.FAILED)
        self.document("Physics", Document.Status.QUARANTINED)
        self.document("Physics", Document.Status.FAILED, archived=True)
        self.document("Physics", Document.Status.COMPLETED)
        self.document("Chemistry", Document.Status.FAILED)
        self.stalled_job("Physics")
        self.stalled_job("Chemistry")
        self.stalled_job("Physics", minutes=5)  # still alive
        self.pending("a", "Physics")
        self.pending("b", "Chemistry")
        self.pending("c", "")
        SignupRequest.objects.filter(username="c").update(
            status=SignupRequest.Status.REJECTED
        )
        self.pending("d", "")

    def test_a_system_administrator_counts_everything(self):
        self.populate()

        self.assertEqual(
            self.counts(self.system_admin),
            {
                "pending_signups": 3,
                "failed_documents": 3,
                "quarantined_documents": 1,
                "stalled_jobs": 2,
            },
        )

    def test_a_department_administrator_counts_their_department(self):
        self.populate()

        self.assertEqual(
            self.counts(self.physics_admin),
            {
                "pending_signups": 1,
                "failed_documents": 2,
                "quarantined_documents": 1,
                "stalled_jobs": 1,
            },
        )

    def test_only_administrators(self):
        lost = make_user("lost_admin", Role.DEPARTMENT_ADMIN, "")
        for user in (self.student, lost):
            response = client_for(user).get(reverse("admin-attention"))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn(
            APIClient().get(reverse("admin-attention")).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_an_answer_is_cached_per_person(self):
        attention.attention_for(self.system_admin)

        with self.assertNumQueries(0):
            attention.attention_for(self.system_admin)
        # Someone else's is their own.
        self.assertEqual(
            attention.attention_for(self.physics_admin)["pending_signups"], 0
        )

    def test_a_sign_up_shows_on_the_next_poll_not_after_the_cache_expires(self):
        self.assertEqual(self.counts(self.system_admin)["pending_signups"], 0)

        with self.captureOnCommitCallbacks(execute=True):
            self.pending("e", "Physics")

        self.assertEqual(self.counts(self.system_admin)["pending_signups"], 1)

    def test_a_finished_run_shows_on_the_next_poll(self):
        job = self.stalled_job("Physics", minutes=5)
        self.assertEqual(self.counts(self.system_admin)["failed_documents"], 0)

        with self.captureOnCommitCallbacks(execute=True):
            Document.objects.filter(pk=job.document_version.document_id).update(
                status=Document.Status.FAILED
            )
            job.status = ProcessingJob.Status.FAILED
            job.save(update_fields=["status", "updated_at"])

        self.assertEqual(self.counts(self.system_admin)["failed_documents"], 1)

    @override_settings(NOTIFICATIONS_ATTENTION_CACHE_SECONDS=0)
    def test_the_cache_can_be_turned_down(self):
        self.counts(self.system_admin)
        self.document("Physics", Document.Status.FAILED)

        self.assertEqual(self.counts(self.system_admin)["failed_documents"], 1)

    def test_a_cache_outage_still_answers(self):
        with mock.patch.object(attention.cache, "get", side_effect=ConnectionError):
            with mock.patch.object(attention.cache, "set", side_effect=ConnectionError):
                self.assertEqual(
                    attention.attention_for(self.system_admin)["pending_signups"], 0
                )


class PruneTests(TestCase):
    def setUp(self):
        self.nia = make_user("nia")

    def aged(self, days, read):
        notification = service.notify(self.nia, "system.note", f"{days}d {read}")
        stamp = timezone.now() - timedelta(days=days)
        Notification.objects.filter(pk=notification.pk).update(
            updated_at=stamp, read_at=stamp if read else None
        )
        return notification

    def remaining(self):
        return set(Notification.objects.values_list("title", flat=True))

    def prune(self, *args):
        out = StringIO()
        call_command("prune_notifications", *args, stdout=out)
        return out.getvalue()

    @override_settings(
        NOTIFICATIONS_RETENTION_DAYS=30, NOTIFICATIONS_UNREAD_RETENTION_DAYS=180
    )
    def test_old_read_and_very_old_unread_go(self):
        for days, read in ((31, True), (29, True), (179, False), (181, False)):
            self.aged(days, read)

        output = self.prune()

        self.assertEqual(self.remaining(), {"29d True", "179d False"})
        self.assertIn("1 read notification(s) deleted.", output)
        self.assertIn("1 unread notification(s) deleted.", output)

    def test_a_dry_run_deletes_nothing(self):
        self.aged(400, True)

        output = self.prune("--dry-run")

        self.assertEqual(len(self.remaining()), 1)
        self.assertIn("1 read notification(s) would be deleted.", output)

    def test_days_from_the_command_line_and_zero_keeps_forever(self):
        self.aged(8, True)
        self.aged(400, False)

        self.prune("--read-days", "7", "--unread-days", "0")

        self.assertEqual(self.remaining(), {"400d False"})

    def test_negative_days_are_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.prune("--read-days", "-1")
