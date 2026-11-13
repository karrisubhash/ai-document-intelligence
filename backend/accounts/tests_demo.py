"""The one-click demo: ephemeral guests that can read and ask, and nothing else."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts import demo
from accounts.middleware import GUEST_ACTIONS, NOTIFICATION_READ_STATE, PUBLIC_DOORS
from accounts.models import Department, UserProfile
from documents.models import AuditLog, Conversation, Document, Message

User = get_user_model()
Role = UserProfile.Role

STRONG = "a-sufficiently-long-passphrase-9"
DEMO_ON = {"DEMO_ENABLED": True, "DEMO_DEPARTMENT": "Demo"}


def make_user(username, role=Role.FACULTY, department="Physics"):
    user = User.objects.create_user(username=username, password=STRONG)
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


def throttle_rates(**rates):
    """SimpleRateThrottle binds THROTTLE_RATES at import, so override it."""
    return mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, rates)


class DemoCase(TestCase):
    def setUp(self):
        # Throttle history lives in the cache, which outlives a test.
        cache.clear()
        Department.objects.create(name="Physics")
        self.demo_department = Department.objects.create(name="Demo")
        self.curator = make_user("curator", Role.DEPARTMENT_ADMIN, "Demo")
        self.sample = Document.objects.create(
            title="Academic Regulations",
            department="Demo",
            owner=self.curator,
            status=Document.Status.COMPLETED,
        )

    def guest_client(self):
        with override_settings(**DEMO_ON):
            guest = demo.create_guest()
        client = APIClient()
        client.force_login(guest)
        return guest, client

    def start(self, client=None, **environ):
        client = client or APIClient(enforce_csrf_checks=True)
        token = client.get(reverse("auth-csrf")).data["csrf_token"]
        return client.post(reverse("auth-demo"), HTTP_X_CSRFTOKEN=token, **environ)


class DemoEndpointTests(DemoCase):
    def test_off_by_default_says_so_and_refuses(self):
        self.assertEqual(
            self.client.get(reverse("auth-demo")).json(), {"enabled": False}
        )
        self.assertEqual(self.start().status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(UserProfile.objects.filter(is_guest=True).exists())

    @override_settings(**DEMO_ON)
    def test_not_offered_until_the_demo_department_has_a_ready_document(self):
        self.sample.status = Document.Status.PROCESSING
        self.sample.save()
        self.assertEqual(
            self.client.get(reverse("auth-demo")).json(), {"enabled": False}
        )
        self.assertEqual(self.start().status_code, status.HTTP_404_NOT_FOUND)

        self.sample.status = Document.Status.COMPLETED
        self.sample.save()
        self.assertEqual(
            self.client.get(reverse("auth-demo")).json(), {"enabled": True}
        )

    @override_settings(**DEMO_ON)
    def test_not_offered_while_its_department_is_inactive(self):
        self.demo_department.is_active = False
        self.demo_department.save()
        self.assertEqual(
            self.client.get(reverse("auth-demo")).json(), {"enabled": False}
        )

    @override_settings(**DEMO_ON)
    def test_starts_a_fresh_guest_signed_in_and_answers_like_a_login(self):
        client = APIClient(enforce_csrf_checks=True)
        response = self.start(client)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        guest = User.objects.get(pk=body["id"])
        self.assertTrue(guest.username.startswith("guest-"))
        self.assertGreater(len(guest.username), len("guest-") + 5)
        self.assertFalse(guest.has_usable_password())
        self.assertTrue(guest.profile.is_guest)
        self.assertEqual(guest.profile.role, Role.STUDENT)
        self.assertEqual(guest.profile.department, "Demo")
        self.assertTrue(body["capabilities"]["is_guest"])
        self.assertFalse(body["capabilities"]["create_collections"])
        self.assertFalse(body["capabilities"]["administer_users"])
        # The same shape as a sign-in.
        self.assertEqual(set(body), set(client.get(reverse("me")).json()))

        # Signed in with an ordinary session, which ends before the account does.
        self.assertEqual(client.get(reverse("me")).json()["username"], guest.username)
        self.assertLessEqual(client.session.get_expiry_age(), demo.session_seconds())

        login = AuditLog.objects.get(action=AuditLog.Action.LOGIN, user=guest)
        self.assertEqual(login.metadata, {"method": "demo"})

    @override_settings(**DEMO_ON)
    def test_each_visitor_gets_their_own_account(self):
        first = self.start().json()["id"]
        second = self.start().json()["id"]
        self.assertNotEqual(first, second)

    @override_settings(**DEMO_ON)
    def test_needs_the_csrf_token_like_a_login(self):
        forged = APIClient(enforce_csrf_checks=True).post(reverse("auth-demo"))
        self.assertEqual(forged.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(UserProfile.objects.filter(is_guest=True).exists())

    @override_settings(**DEMO_ON)
    def test_rate_limited_per_address_and_overall(self):
        with throttle_rates(demo="1/hour", demo_global="100/hour"):
            self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)
            again = self.start()
            elsewhere = self.start(REMOTE_ADDR="10.0.0.9")
        self.assertEqual(again.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(elsewhere.status_code, status.HTTP_201_CREATED)

        cache.clear()
        with throttle_rates(demo="100/hour", demo_global="1/hour"):
            self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)
            busy = self.start(REMOTE_ADDR="10.0.0.10")
        self.assertEqual(busy.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(**DEMO_ON, DEMO_MAX_GUESTS=1)
    def test_says_it_is_full_at_the_ceiling(self):
        self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)
        full = self.start()
        self.assertEqual(full.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(
            full.json()["detail"].split(" ")[:4], ["The", "demo", "is", "full"]
        )
        self.assertEqual(UserProfile.objects.filter(is_guest=True).count(), 1)

    def test_me_reports_who_is_a_guest(self):
        member = make_user("asha", Role.STUDENT)
        client = APIClient()
        client.force_authenticate(member)
        self.assertIs(
            client.get(reverse("me")).json()["capabilities"]["is_guest"], False
        )

        _, guest_client = self.guest_client()
        self.assertIs(
            guest_client.get(reverse("me")).json()["capabilities"]["is_guest"], True
        )


class GuestGuardTests(DemoCase):
    """Every class of write a member can make, refused for a guest - by the
    guard, not by whichever check the view happens to have."""

    def setUp(self):
        super().setUp()
        self.guest, self.client = self.guest_client()
        self.conversation = Conversation.objects.create(
            owner=self.guest, department="Demo"
        )

    def file(self):
        return SimpleUploadedFile(
            "notes.txt", b"Some notes.", content_type="text/plain"
        )

    def writes(self):
        doc = self.sample.pk
        conversation = self.conversation.pk
        return [
            (
                "upload",
                "post",
                "/api/v1/documents/",
                {"title": "Mine", "file": self.file()},
                "multipart",
            ),
            (
                "upload (alias)",
                "post",
                "/api/documents/",
                {"title": "Mine", "file": self.file()},
                "multipart",
            ),
            (
                "new version",
                "post",
                f"/api/v1/documents/{doc}/versions/",
                {"file": self.file()},
                "multipart",
            ),
            (
                "edit",
                "patch",
                f"/api/v1/documents/{doc}/",
                {"title": "Renamed"},
                "multipart",
            ),
            ("delete", "delete", f"/api/v1/documents/{doc}/", None, None),
            ("archive", "post", f"/api/v1/documents/{doc}/archive/", None, None),
            ("reprocess", "post", f"/api/v1/documents/{doc}/reprocess/", None, None),
            (
                "bulk delete",
                "post",
                "/api/v1/documents/bulk-delete/",
                {"ids": [doc]},
                "json",
            ),
            (
                "new collection",
                "post",
                "/api/v1/collections/",
                {"name": "Mine"},
                "json",
            ),
            (
                "password",
                "post",
                "/api/v1/auth/password/",
                {"current_password": "x", "new_password": STRONG},
                "json",
            ),
            (
                "rename conversation",
                "patch",
                f"/api/v1/chat/conversations/{conversation}/",
                {"title": "x"},
                "json",
            ),
            (
                "delete conversation",
                "delete",
                f"/api/v1/chat/conversations/{conversation}/",
                None,
                None,
            ),
            (
                "bulk delete conversations",
                "post",
                "/api/v1/chat/conversations/bulk-delete/",
                {"all": True},
                "json",
            ),
            (
                "create account",
                "post",
                "/api/v1/admin/users/",
                {"username": "x", "password": STRONG},
                "json",
            ),
            (
                "create department",
                "post",
                "/api/v1/admin/departments/",
                {"name": "X"},
                "json",
            ),
            ("retry job", "post", "/api/v1/admin/jobs/1/retry/", None, None),
            (
                "load samples",
                "post",
                "/api/v1/admin/samples/",
                {"department": "Demo"},
                "json",
            ),
        ]

    def test_every_write_is_refused(self):
        for label, method, path, body, form in self.writes():
            with self.subTest(label):
                kwargs = {"format": form} if form else {}
                response = getattr(self.client, method)(path, body, **kwargs)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, path)
                self.assertEqual(response.json()["code"], "demo_read_only", path)

        # And none of them changed anything.
        self.sample.refresh_from_db()
        self.assertEqual(self.sample.title, "Academic Regulations")
        self.assertIsNone(self.sample.archived_at)
        self.assertEqual(Document.objects.count(), 1)
        self.assertTrue(Conversation.objects.filter(pk=self.conversation.pk).exists())
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_reads_still_work(self):
        self.assertEqual(self.client.get("/api/v1/documents/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/v1/documents/{self.sample.pk}/").status_code, 200
        )
        self.assertEqual(
            self.client.get("/api/v1/chat/conversations/").status_code, 200
        )

    def test_a_member_is_untouched_by_the_guard(self):
        member = make_user("ravi", Role.FACULTY, "Physics")
        client = APIClient()
        client.force_login(member)
        conversation = Conversation.objects.create(owner=member, department="Physics")
        response = client.patch(
            f"/api/v1/chat/conversations/{conversation.pk}/",
            {"title": "Mine"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_can_search_ask_rate_the_answer_and_sign_out(self):
        search = self.client.post(
            "/api/v1/search/", {"query": "attendance"}, format="json"
        )
        self.assertEqual(search.status_code, status.HTTP_200_OK)

        asked = self.client.post(
            "/api/v1/chat/", {"question": "What is the attendance rule?"}, format="json"
        )
        self.assertEqual(asked.status_code, status.HTTP_200_OK)
        rated = self.client.post(
            f"/api/v1/messages/{asked.json()['message_id']}/feedback/",
            {"rating": "HELPFUL"},
            format="json",
        )
        self.assertEqual(rated.status_code, status.HTTP_201_CREATED)

        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 204)
        # Signed out: no credentials at all is a 401.
        self.assertEqual(
            self.client.get(reverse("me")).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_the_public_doors_stay_open(self):
        """A guest pressing Sign in for a real account in another tab, or
        starting a fresh demo, is not stuck behind the guest session."""
        make_user("asha", Role.STUDENT)
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": "asha", "password": STRONG},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(reverse("me")).json()["username"], "asha")

    def test_notification_read_state_is_let_through(self):
        # The notifications app is not part of this branch: the route does
        # not resolve, so the guard never sees it - but its documented paths
        # are on the allowlist, and nothing else under it is.
        for path in ("/api/v1/notifications/12/read/", "/api/notifications/read-all/"):
            self.assertTrue(NOTIFICATION_READ_STATE.match(path), path)
        for path in ("/api/v1/notifications/12/", "/api/v1/notifications/12/read/x/"):
            self.assertFalse(NOTIFICATION_READ_STATE.match(path), path)

    def test_the_allowlist_is_only_what_the_brief_allows(self):
        self.assertEqual(
            set(GUEST_ACTIONS),
            {"chat", "chat-stream", "search", "message-feedback", "auth-logout"},
        )
        self.assertEqual(
            PUBLIC_DOORS,
            {"auth-login", "auth-demo", "auth-signup", "api-token-auth", "auth-csrf"},
        )

    def test_a_guest_cannot_use_an_api_token(self):
        token = Token.objects.create(user=self.guest)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(
            client.get(reverse("me")).status_code, status.HTTP_401_UNAUTHORIZED
        )
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())

    def test_guests_never_see_one_another_s_conversations(self):
        asked = self.client.post(
            "/api/v1/chat/", {"question": "Attendance?"}, format="json"
        )
        mine = asked.json()["conversation_id"]

        _, other = self.guest_client()
        self.assertEqual(other.get("/api/v1/chat/conversations/").json()["count"], 0)
        self.assertEqual(
            other.get(f"/api/v1/chat/conversations/{mine}/").status_code, 404
        )
        message = Message.objects.filter(conversation_id=mine, role="ASSISTANT").first()
        rated = other.post(
            f"/api/v1/messages/{message.pk}/feedback/",
            {"rating": "HELPFUL"},
            format="json",
        )
        self.assertEqual(rated.status_code, status.HTTP_404_NOT_FOUND)


class GuestBudgetTests(DemoCase):
    def test_a_guest_s_questions_have_a_budget_a_member_does_not_share(self):
        _, guest = self.guest_client()
        member = make_user("asha", Role.STUDENT, "Demo")
        member_client = APIClient()
        member_client.force_login(member)
        ask = {"question": "Attendance?"}
        with throttle_rates(guest_chat="1/hour"):
            self.assertEqual(
                guest.post("/api/v1/chat/", ask, format="json").status_code, 200
            )
            refused = guest.post("/api/v1/chat/stream/", ask, format="json")
            for _ in range(2):
                self.assertEqual(
                    member_client.post("/api/v1/chat/", ask, format="json").status_code,
                    200,
                )

        self.assertEqual(refused.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(refused.json()["code"], "demo_throttled")
        self.assertGreater(int(refused["Retry-After"]), 0)

    def test_all_guests_together_have_a_ceiling(self):
        _, first = self.guest_client()
        _, second = self.guest_client()
        with throttle_rates(guest_search_global="1/hour"):
            body = {"query": "attendance"}
            self.assertEqual(
                first.post("/api/v1/search/", body, format="json").status_code, 200
            )
            refused = second.post("/api/v1/search/", body, format="json")
        self.assertEqual(refused.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("busy", refused.json()["detail"])


class PurgeTests(DemoCase):
    def age(self, user, hours):
        User.objects.filter(pk=user.pk).update(
            date_joined=timezone.now() - timedelta(hours=hours)
        )

    @override_settings(DEMO_GUEST_TTL_HOURS=24)
    def test_deletes_expired_guests_with_their_data_and_nothing_else(self):
        # Both made before either is aged: making a guest sweeps expired ones.
        old, old_client = self.guest_client()
        young, _ = self.guest_client()
        asked = old_client.post(
            "/api/v1/chat/", {"question": "Attendance?"}, format="json"
        )
        old_client.post(
            f"/api/v1/messages/{asked.json()['message_id']}/feedback/",
            {"rating": "HELPFUL"},
            format="json",
        )
        self.age(old, 25)
        self.age(young, 23)
        veteran = make_user("veteran", Role.STUDENT, "Demo")
        self.age(veteran, 24 * 400)

        out = StringIO()
        call_command("purge_demo_guests", stdout=out)

        self.assertFalse(User.objects.filter(pk=old.pk).exists())
        self.assertFalse(Conversation.objects.filter(owner_id=old.pk).exists())
        self.assertFalse(
            Message.objects.filter(
                conversation_id=asked.json()["conversation_id"]
            ).exists()
        )
        self.assertTrue(User.objects.filter(pk=young.pk).exists())
        self.assertTrue(User.objects.filter(pk=veteran.pk).exists())
        self.assertTrue(Document.objects.filter(pk=self.sample.pk).exists())
        self.assertIn("Deleted 1 demo guest", out.getvalue())
        # The trail stays, with the account cleared.
        self.assertEqual(
            AuditLog.objects.filter(
                user__isnull=True, action=AuditLog.Action.CHAT
            ).count(),
            1,
        )
        purge = AuditLog.objects.get(action=AuditLog.Action.GUEST_PURGE)
        self.assertEqual(purge.metadata["deleted"], 1)
        self.assertEqual(purge.metadata["trigger"], "command")

    def test_dry_run_deletes_nothing(self):
        old, _ = self.guest_client()
        self.age(old, 48)
        out = StringIO()
        call_command("purge_demo_guests", "--dry-run", stdout=out)
        self.assertTrue(User.objects.filter(pk=old.pk).exists())
        self.assertIn("1 demo guest account(s)", out.getvalue())
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.GUEST_PURGE).exists()
        )

    @override_settings(**DEMO_ON)
    def test_a_new_guest_sweeps_a_bounded_number_of_expired_ones(self):
        expired = [self.guest_client()[0] for _ in range(3)]
        for guest in expired:
            self.age(guest, 30)
        with mock.patch.object(demo, "OPPORTUNISTIC_PURGE_LIMIT", 2):
            self.assertEqual(self.start().status_code, status.HTTP_201_CREATED)
        remaining = User.objects.filter(pk__in=[guest.pk for guest in expired]).count()
        self.assertEqual(remaining, 1)
        purge = AuditLog.objects.get(action=AuditLog.Action.GUEST_PURGE)
        self.assertEqual(purge.metadata["trigger"], "demo")

    def test_nothing_to_sweep_writes_no_row(self):
        self.guest_client()
        self.assertEqual(demo.purge_expired_guests(), 0)
        self.assertFalse(
            AuditLog.objects.filter(action=AuditLog.Action.GUEST_PURGE).exists()
        )


class GuestAdministrationTests(DemoCase):
    def setUp(self):
        super().setUp()
        self.guest, _ = self.guest_client()
        self.admin = make_user("root", Role.SYSTEM_ADMIN, "")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_guests_are_not_listed_or_administrable(self):
        listed = [
            row["username"]
            for row in self.client.get("/api/v1/admin/users/").json()["results"]
        ]
        self.assertNotIn(self.guest.username, listed)
        self.assertIn("curator", listed)
        response = self.client.post(
            f"/api/v1/admin/users/{self.guest.pk}/set-password/",
            {"password": STRONG},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(User.objects.get(pk=self.guest.pk).has_usable_password())

    def test_guests_are_not_counted_as_department_members(self):
        rows = self.client.get("/api/v1/admin/departments/").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        demo_row = next(row for row in rows if row["name"] == "Demo")
        self.assertEqual(demo_row["member_count"], 1)  # the curator

    def test_the_guest_prefix_is_kept_for_guests(self):
        made = self.client.post(
            "/api/v1/admin/users/",
            {"username": "guest-imposter", "password": STRONG, "department": "Physics"},
            format="json",
        )
        self.assertEqual(made.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", made.json())

        applied = APIClient().post(
            reverse("auth-signup"),
            {
                "username": "Guest-x1",
                "email": "x@example.edu",
                "first_name": "X",
                "password": STRONG,
                "department": "Physics",
            },
            format="json",
        )
        self.assertEqual(applied.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", applied.json())

        # Account creation keeps a name as typed: its full-width spelling
        # would read as "guest-..." in every listing.
        lookalike = self.client.post(
            "/api/v1/admin/users/",
            {
                "username": "ｇｕｅｓｔ-imposter",
                "password": STRONG,
                "department": "Physics",
            },
            format="json",
        )
        self.assertEqual(lookalike.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", lookalike.json())
