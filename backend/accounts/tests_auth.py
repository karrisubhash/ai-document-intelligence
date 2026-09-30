"""Browser sessions, expiring API tokens, logout and password change."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import UserProfile
from documents.models import AuditLog

User = get_user_model()

PASSWORD = "correct-horse-battery-staple"


def make_user(username="alice", role=UserProfile.Role.FACULTY, department="Physics"):
    user = User.objects.create_user(username=username, password=PASSWORD)
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


class SessionLoginTests(TestCase):
    def setUp(self):
        # Throttle counters live in the cache, which outlives each test.
        cache.clear()
        self.user = make_user()
        # CSRF enforced, the way a real browser session is.
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf(self):
        return self.client.get(reverse("auth-csrf")).data["csrf_token"]

    def login(self, password=PASSWORD, username="alice", token=None):
        return self.client.post(
            reverse("auth-login"),
            {"username": username, "password": password},
            format="json",
            HTTP_X_CSRFTOKEN=token if token is not None else self.csrf(),
        )

    def test_login_sets_an_httponly_session_and_returns_the_profile(self):
        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "alice")
        self.assertIn("capabilities", response.data)
        cookie = response.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        # No token in the body: nothing script-readable is issued.
        self.assertNotIn("token", response.data)

        self.assertEqual(self.client.get(reverse("me")).status_code, 200)

    def test_login_without_a_csrf_token_is_refused(self):
        """A forged login would sign the victim into the attacker's account."""
        response = self.client.post(
            reverse("auth-login"),
            {"username": "alice", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_bad_credentials_get_one_message_whoever_the_user_is(self):
        wrong_password = self.login(password="nope")
        no_such_user = self.login(username="mallory")

        self.assertEqual(wrong_password.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(no_such_user.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(wrong_password.data, no_such_user.data)

    def test_failed_logins_are_audited_against_the_target(self):
        self.login(password="nope")
        self.login(username="mallory")

        self.assertTrue(
            AuditLog.objects.filter(
                user=self.user, action=AuditLog.Action.LOGIN_FAILED
            ).exists()
        )
        unknown = AuditLog.objects.get(
            user__isnull=True, action=AuditLog.Action.LOGIN_FAILED
        )
        self.assertEqual(unknown.metadata["attempted_username"], "mallory")
        self.assertNotIn("password", str(unknown.metadata).lower())

    def test_failed_logins_are_counted_where_monitoring_looks(self):
        """A 400 is not a 401, so the middleware alone never counted these."""
        from documents.observability import metrics

        before = metrics.auth_failures._value.get()
        self.login(password="nope")
        self.login(username="mallory")
        self.assertEqual(metrics.auth_failures._value.get() - before, 2)

    def test_an_inactive_account_cannot_sign_in(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.login().status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unsafe_request_on_a_session_needs_the_csrf_header(self):
        self.login()
        token = self.client.cookies["csrftoken"].value

        refused = self.client.post(reverse("auth-logout"))
        accepted = self.client.post(reverse("auth-logout"), HTTP_X_CSRFTOKEN=token)

        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(accepted.status_code, status.HTTP_204_NO_CONTENT)

    def test_logout_ends_the_session(self):
        self.login()
        token = self.client.cookies["csrftoken"].value
        self.client.post(reverse("auth-logout"), HTTP_X_CSRFTOKEN=token)

        self.assertIn(
            self.client.get(reverse("me")).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.user, action=AuditLog.Action.LOGOUT
            ).exists()
        )


class ExpiringTokenTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = APIClient()

    def issue(self):
        response = self.client.post(
            reverse("api-token-auth"), {"username": "alice", "password": PASSWORD}
        )
        return response.data["token"]

    def test_a_fresh_token_authenticates(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.issue()}")
        self.assertEqual(self.client.get(reverse("me")).status_code, 200)

    def test_an_expired_token_is_refused_and_deleted(self):
        key = self.issue()
        Token.objects.filter(key=key).update(
            created=timezone.now() - timedelta(hours=13)
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {key}")

        response = self.client.get(reverse("me"))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Token.objects.filter(key=key).exists())

    def test_signing_in_again_replaces_an_expired_token(self):
        """get_or_create would hand back the expired row it just refused."""
        old = self.issue()
        Token.objects.filter(key=old).update(
            created=timezone.now() - timedelta(hours=13)
        )
        self.assertNotEqual(self.issue(), old)

    def test_logout_revokes_the_token(self):
        key = self.issue()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {key}")

        self.assertEqual(
            self.client.post(reverse("auth-logout")).status_code,
            status.HTTP_204_NO_CONTENT,
        )
        self.assertFalse(Token.objects.filter(key=key).exists())


class PasswordChangeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def change(self, current, new):
        return self.client.post(
            reverse("auth-password"),
            {"current_password": current, "new_password": new},
            format="json",
        )

    def test_the_current_password_is_required(self):
        response = self.change("wrong", "a-much-better-passphrase-2026")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", response.data)

    def test_a_weak_password_is_reported_against_its_field(self):
        response = self.change(PASSWORD, "123")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.data)

    def test_a_change_revokes_api_tokens_and_is_audited(self):
        Token.objects.create(user=self.user)
        response = self.change(PASSWORD, "a-much-better-passphrase-2026")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-much-better-passphrase-2026"))
        self.assertFalse(Token.objects.filter(user=self.user).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.user, action=AuditLog.Action.PASSWORD_CHANGE
            ).exists()
        )


class CapabilityTests(TestCase):
    def capabilities(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.get(reverse("me")).data["capabilities"]

    def test_an_ordinary_user_is_offered_no_administration(self):
        caps = self.capabilities(make_user("student", UserProfile.Role.STUDENT))
        self.assertFalse(any(caps.values()))

    def test_a_department_admin_administers_people_not_the_system(self):
        caps = self.capabilities(make_user("dadmin", UserProfile.Role.DEPARTMENT_ADMIN))
        self.assertTrue(caps["administer_users"])
        self.assertTrue(caps["view_audit_log"])
        self.assertFalse(caps["administer_departments"])
        self.assertFalse(caps["view_system_status"])

    def test_a_system_admin_has_everything(self):
        caps = self.capabilities(make_user("sadmin", UserProfile.Role.SYSTEM_ADMIN))
        self.assertTrue(all(caps.values()))


class ThrottlingBehindAProxyTests(TestCase):
    """Throttles that looked right in isolation and failed behind nginx."""

    def setUp(self):
        cache.clear()

    def test_the_csrf_endpoint_is_not_rate_limited(self):
        """Anonymous, so the anonymous throttle applied - and behind nginx
        every browser shares one address. Sign-in would have stopped working
        site-wide after thirty page loads an hour."""
        client = APIClient()
        for _ in range(60):
            self.assertEqual(client.get(reverse("auth-csrf")).status_code, 200)

    def test_a_polling_dashboard_is_not_throttled_within_the_hour(self):
        """The dashboard polls every 5 s while documents process: 720 an
        hour, against the old 300/hour catch-all."""
        client = APIClient()
        client.force_authenticate(make_user("poller"))
        for _ in range(750):
            response = client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_client_addresses_are_read_through_one_proxy_when_configured(self):
        from django.test import RequestFactory, override_settings
        from rest_framework.request import Request

        from documents.throttling import LoginRateThrottle

        raw = RequestFactory().post(
            "/api/auth/login/",
            REMOTE_ADDR="172.18.0.5",  # nginx
            HTTP_X_FORWARDED_FOR="203.0.113.7",  # the browser
        )
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1}):
            self.assertEqual(LoginRateThrottle().get_ident(Request(raw)), "203.0.113.7")
        # With no proxy configured, the socket address is used.
        self.assertEqual(LoginRateThrottle().get_ident(Request(raw)), "172.18.0.5")

    def test_a_forged_forwarded_for_header_does_not_change_who_you_are(self):
        """The bypass: DRF's unset default reads the WHOLE header, which the
        client writes. Varying it gave every login attempt a fresh throttle
        key. nginx appends the real address; only that entry may count."""
        from django.test import RequestFactory, override_settings
        from rest_framework.request import Request

        from documents.throttling import LoginRateThrottle

        def ident(forged, *, proxies):
            raw = RequestFactory().post(
                "/api/auth/login/",
                REMOTE_ADDR="172.18.0.5",
                # What nginx forwards: the client's forged value, then the
                # address it actually saw.
                HTTP_X_FORWARDED_FOR=f"{forged}, 203.0.113.7",
            )
            with override_settings(REST_FRAMEWORK={"NUM_PROXIES": proxies}):
                return LoginRateThrottle().get_ident(Request(raw))

        self.assertEqual(ident("10.0.0.1", proxies=1), ident("10.9.9.9", proxies=1))
        self.assertEqual(ident("10.0.0.1", proxies=1), "203.0.113.7")
        # And with no proxy, the header is ignored entirely.
        self.assertEqual(ident("10.0.0.1", proxies=0), "172.18.0.5")

    def test_the_setting_is_never_left_to_drfs_trusting_default(self):
        from rest_framework.settings import api_settings

        self.assertIsNotNone(api_settings.NUM_PROXIES)
