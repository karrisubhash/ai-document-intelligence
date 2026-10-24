"""Self-service sign-up: anyone may ask, only an administrator lets them in."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Department, SignupRequest, UserProfile
from accounts.services import rename_department
from documents.models import AuditLog
from documents.throttling import SignupRateThrottle

User = get_user_model()
Role = UserProfile.Role

STRONG = "a-sufficiently-long-passphrase-9"


def make_user(username, role=Role.FACULTY, department="Physics"):
    user = User.objects.create_user(username=username, password=STRONG)
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


def application(**overrides):
    body = {
        "username": "newcomer",
        "email": "newcomer@example.edu",
        "first_name": "Nia",
        "last_name": "Okafor",
        "password": STRONG,
        "department": "Physics",
        "role": Role.STUDENT,
        "note": "First-year, starting this term.",
    }
    body.update(overrides)
    return body


def pending(username="newcomer", department="Physics", **fields):
    from django.contrib.auth.hashers import make_password

    return SignupRequest.objects.create(
        username=username,
        email=f"{username}@example.edu",
        first_name=username.title(),
        password=make_password(STRONG),
        department=department,
        **fields,
    )


class SignupTestCase(TestCase):
    def setUp(self):
        # Throttle counters live in the cache, which outlives each test.
        cache.clear()
        Department.objects.create(name="Physics", code="PHY")
        Department.objects.create(name="Chemistry", code="CHE")
        Department.objects.create(name="Classics", is_active=False)
        # CSRF enforced, the way a real browser is.
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf(self):
        return self.client.get(reverse("auth-csrf")).data["csrf_token"]

    def apply(self, **overrides):
        return self.client.post(
            reverse("auth-signup"),
            application(**overrides),
            format="json",
            HTTP_X_CSRFTOKEN=self.csrf(),
        )

    def login(self, username="newcomer", password=STRONG):
        return self.client.post(
            reverse("auth-login"),
            {"username": username, "password": password},
            format="json",
            HTTP_X_CSRFTOKEN=self.csrf(),
        )


class SignupFormTests(SignupTestCase):
    def test_the_form_offers_active_departments_and_no_administrative_role(self):
        response = self.client.get(reverse("auth-signup"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["enabled"])
        self.assertEqual(
            [row["name"] for row in response.data["departments"]],
            ["Chemistry", "Physics"],
        )
        self.assertEqual(
            {row["value"] for row in response.data["roles"]},
            {Role.FACULTY, Role.STAFF, Role.STUDENT},
        )

    @override_settings(SIGNUP_ENABLED=False)
    def test_turned_off_it_offers_nothing_and_accepts_nothing(self):
        described = self.client.get(reverse("auth-signup"))
        filed = self.apply()

        self.assertEqual(
            described.data, {"enabled": False, "departments": [], "roles": []}
        )
        self.assertEqual(filed.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SignupRequest.objects.exists())

    def test_reading_the_form_is_not_rate_limited(self):
        """Every visit to the sign-in page reads it, and behind nginx every
        browser shares one address."""
        for _ in range(60):
            self.assertEqual(self.client.get(reverse("auth-signup")).status_code, 200)


class SignupSubmissionTests(SignupTestCase):
    def test_a_request_waits_and_creates_no_account(self):
        response = self.apply()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data, {"status": "PENDING"})
        signup = SignupRequest.objects.get()
        self.assertEqual(signup.status, SignupRequest.Status.PENDING)
        self.assertEqual(
            (signup.department, signup.role, signup.note),
            ("Physics", Role.STUDENT, "First-year, starting this term."),
        )
        self.assertFalse(User.objects.filter(username="newcomer").exists())

    def test_the_password_is_kept_only_as_a_hash(self):
        self.apply()

        signup = SignupRequest.objects.get()
        self.assertNotIn(STRONG, signup.password)
        self.assertTrue(check_password(STRONG, signup.password))

    def test_the_request_is_audited_for_its_department(self):
        self.apply()

        row = AuditLog.objects.get(action=AuditLog.Action.SIGNUP_REQUEST)
        self.assertIsNone(row.user)
        self.assertEqual(row.document_department, "Physics")
        self.assertEqual(row.metadata["username"], "newcomer")
        self.assertNotIn(STRONG, str(row.metadata))

    def test_a_request_without_a_csrf_token_is_refused(self):
        response = self.client.post(
            reverse("auth-signup"), application(), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(SignupRequest.objects.exists())

    def test_a_name_an_account_holds_is_refused_whatever_its_case(self):
        make_user("newcomer")

        response = self.apply(username="NewComer")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.data)

    def test_a_waiting_request_holds_its_name_with_the_same_answer(self):
        make_user("taken")
        pending("waiting")

        by_account = self.apply(username="taken")
        by_request = self.apply(username="Waiting")

        self.assertEqual(by_request.status_code, status.HTTP_400_BAD_REQUEST)
        # Which of the two holds a name is nobody else's business.
        self.assertEqual(by_account.data["username"], by_request.data["username"])

    def test_a_declined_applicant_may_apply_again(self):
        pending(status=SignupRequest.Status.REJECTED)

        response = self.apply()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_two_waiting_requests_for_one_name_cannot_coexist(self):
        """The serializer's check races; the constraint does not."""
        pending("alice")
        with self.assertRaises(IntegrityError), transaction.atomic():
            pending("Alice")

    def test_losing_that_race_is_a_400_not_a_500(self):
        pending("alice")
        with mock.patch(
            "accounts.api.serializers.username_unavailable", return_value=False
        ):
            response = self.apply(username="Alice")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.data)

    def test_the_password_rules_apply(self):
        for password in ("short", "newcomer1", "12345678901234"):
            with self.subTest(password=password):
                response = self.apply(password=password)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("password", response.data)
        self.assertFalse(SignupRequest.objects.exists())

    def test_the_department_goes_through_the_registry(self):
        unknown = self.apply(department="Astrology")
        inactive = self.apply(department="Classics", username="other")
        respelled = self.apply(department="  physics ", username="third")

        self.assertEqual(unknown.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(inactive.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(respelled.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SignupRequest.objects.get().department, "Physics")

    def test_no_department_is_allowed_for_a_system_admin_to_place(self):
        response = self.apply(department="")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(SignupRequest.objects.get().department, "")

    def test_an_administrative_role_cannot_be_requested(self):
        for role in (Role.SYSTEM_ADMIN, Role.DEPARTMENT_ADMIN):
            with self.subTest(role=role):
                response = self.apply(role=role)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("role", response.data)

    def test_a_username_django_would_refuse_is_refused(self):
        response = self.apply(username="no spaces!")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("username", response.data)

    def test_requests_are_rate_limited_per_address(self):
        with mock.patch.object(
            SignupRateThrottle, "THROTTLE_RATES", {"signup": "2/hour"}
        ):
            first = self.apply(username="one")
            second = self.apply(username="two")
            third = self.apply(username="three")

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(third.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(SIGNUP_MAX_PENDING=1)
    def test_the_waiting_queue_is_bounded(self):
        self.apply(username="one")
        response = self.apply(username="two")

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(SignupRequest.objects.count(), 1)


class SigningInBeforeApprovalTests(SignupTestCase):
    def test_a_waiting_applicant_is_told_their_request_is_waiting(self):
        pending()

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "signup_pending")
        self.assertNotIn("sessionid", response.cookies)

    def test_without_the_password_nobody_learns_who_has_applied(self):
        pending()

        wrong_password = self.login(password="not-the-password")
        nobody = self.login(username="nobody")

        self.assertEqual(wrong_password.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(wrong_password.data, nobody.data)

    def test_a_declined_applicant_is_told_and_given_the_reason(self):
        pending(
            status=SignupRequest.Status.REJECTED,
            decision_note="Use your university email address.",
        )

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "signup_rejected")
        self.assertIn("Use your university email address.", response.data["detail"])

    def test_an_account_holding_the_name_is_not_answered_for_by_an_old_request(self):
        """A failed sign-in to a real account is that account's, whatever an
        earlier, declined applicant for the same name typed."""
        pending(status=SignupRequest.Status.REJECTED)
        User.objects.create_user(username="newcomer", password="someone-elses-9876")

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_token_endpoint_says_the_same(self):
        pending()

        response = self.client.post(
            reverse("api-token-auth"),
            {"username": "newcomer", "password": STRONG},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["code"], "signup_pending")


class ReviewTestCase(SignupTestCase):
    def setUp(self):
        super().setUp()
        self.system_admin = make_user("root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user("phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        self.physics_student = make_user("phy_student", Role.STUDENT, "Physics")

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def approve(self, actor, signup, **body):
        return self.as_user(actor).post(
            reverse("admin-signup-request-approve", args=[signup.id]),
            body,
            format="json",
        )

    def reject(self, actor, signup, **body):
        return self.as_user(actor).post(
            reverse("admin-signup-request-reject", args=[signup.id]),
            body,
            format="json",
        )


class ReviewVisibilityTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.physics = pending("phy_applicant", "Physics")
        self.chemistry = pending("che_applicant", "Chemistry")
        self.unplaced = pending("unplaced", "")

    def listed(self, actor, **params):
        response = self.as_user(actor).get(reverse("admin-signup-request-list"), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["username"] for row in response.data["results"]}

    def test_a_system_admin_reviews_every_request(self):
        self.assertEqual(
            self.listed(self.system_admin),
            {"phy_applicant", "che_applicant", "unplaced"},
        )

    def test_a_department_admin_reviews_their_department_only(self):
        self.assertEqual(self.listed(self.physics_admin), {"phy_applicant"})

    def test_another_departments_request_is_invisible_not_forbidden(self):
        response = self.approve(self.physics_admin, self.chemistry)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(User.objects.filter(username="che_applicant").exists())

    def test_an_ordinary_user_cannot_review(self):
        response = self.as_user(self.physics_student).get(
            reverse("admin-signup-request-list")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_listing_filters_by_status(self):
        self.chemistry.status = SignupRequest.Status.REJECTED
        self.chemistry.save()

        self.assertEqual(
            self.listed(self.system_admin, status="PENDING"),
            {"phy_applicant", "unplaced"},
        )
        self.assertEqual(
            self.listed(self.system_admin, status="REJECTED"), {"che_applicant"}
        )

    def test_the_password_hash_is_never_listed(self):
        response = self.as_user(self.system_admin).get(
            reverse("admin-signup-request-list")
        )
        for row in response.data["results"]:
            self.assertNotIn("password", row)

    def test_a_department_admin_sees_their_departments_requests_in_the_audit_log(self):
        self.apply(username="audited", department="Physics")
        self.apply(username="elsewhere", department="Chemistry")

        response = self.as_user(self.physics_admin).get(
            reverse("admin-audit"), {"action": "SIGNUP_REQUEST"}
        )

        usernames = [row["metadata"]["username"] for row in response.data["results"]]
        self.assertEqual(usernames, ["audited"])

    def test_the_dashboard_counts_what_this_administrator_reviews(self):
        def count(user):
            return (
                self.as_user(user)
                .get(reverse("dashboard"))
                .data["pending_signup_requests"]
            )

        self.assertEqual(count(self.system_admin), 3)
        self.assertEqual(count(self.physics_admin), 1)
        self.assertIsNone(count(self.physics_student))


class ApprovalTests(ReviewTestCase):
    def test_approval_creates_the_account_they_asked_for(self):
        signup = pending(role=Role.FACULTY, last_name="Okafor")

        response = self.approve(self.physics_admin, signup)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        user = User.objects.get(username="newcomer")
        self.assertTrue(user.is_active)
        self.assertEqual(
            (user.email, user.first_name, user.last_name),
            ("newcomer@example.edu", "Newcomer", "Okafor"),
        )
        self.assertEqual(
            (user.profile.role, user.profile.department), (Role.FACULTY, "Physics")
        )
        signup.refresh_from_db()
        self.assertEqual(signup.status, SignupRequest.Status.APPROVED)
        self.assertEqual((signup.user, signup.reviewed_by), (user, self.physics_admin))
        self.assertIsNotNone(signup.reviewed_at)
        self.assertEqual(response.data["user"], user.id)

    def test_they_sign_in_with_the_password_they_chose(self):
        signup = pending()
        self.approve(self.physics_admin, signup)

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["username"], "newcomer")

    def test_the_request_lets_go_of_the_hash_once_the_account_has_it(self):
        signup = pending()
        self.approve(self.physics_admin, signup)

        signup.refresh_from_db()
        self.assertEqual(signup.password, "")

    def test_approval_is_audited_with_the_new_account(self):
        signup = pending()
        self.approve(self.physics_admin, signup)

        row = AuditLog.objects.get(action=AuditLog.Action.SIGNUP_APPROVE)
        self.assertEqual(row.user, self.physics_admin)
        self.assertEqual(row.document_department, "Physics")
        self.assertEqual(
            row.metadata["target_user_id"], User.objects.get(username="newcomer").id
        )
        self.assertEqual(row.metadata["role"], Role.STUDENT)

    def test_a_system_admin_may_place_them_elsewhere(self):
        signup = pending(department="")

        response = self.approve(
            self.system_admin, signup, role=Role.STAFF, department="chemistry"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        profile = User.objects.get(username="newcomer").profile
        self.assertEqual((profile.role, profile.department), (Role.STAFF, "Chemistry"))

    def test_a_department_admin_grants_no_more_than_they_could_by_hand(self):
        signup = pending()

        as_admin = self.approve(self.physics_admin, signup, role=Role.DEPARTMENT_ADMIN)
        elsewhere = self.approve(self.physics_admin, signup, department="Chemistry")

        self.assertEqual(as_admin.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("role", as_admin.data)
        self.assertEqual(elsewhere.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("department", elsewhere.data)
        self.assertFalse(User.objects.filter(username="newcomer").exists())

    def test_a_department_retired_since_the_request_accepts_no_one(self):
        signup = pending(department="Physics")
        Department.objects.filter(name="Physics").update(is_active=False)

        response = self.approve(self.system_admin, signup)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("department", response.data)

    def test_a_request_is_decided_once(self):
        signup = pending()
        self.approve(self.physics_admin, signup)

        again = self.approve(self.system_admin, signup)
        then_reject = self.reject(self.system_admin, signup)

        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(then_reject.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(User.objects.filter(username="newcomer").count(), 1)

    def test_a_name_taken_since_the_request_is_a_conflict(self):
        signup = pending()
        make_user("NewComer")

        response = self.approve(self.system_admin, signup)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        signup.refresh_from_db()
        self.assertEqual(signup.status, SignupRequest.Status.PENDING)
        self.assertFalse(User.objects.filter(username="newcomer").exists())


class RejectionTests(ReviewTestCase):
    def test_a_rejection_creates_nothing_and_keeps_the_reason(self):
        signup = pending()

        response = self.reject(
            self.physics_admin, signup, note="Use your university email address."
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        signup.refresh_from_db()
        self.assertEqual(signup.status, SignupRequest.Status.REJECTED)
        self.assertEqual(signup.decision_note, "Use your university email address.")
        self.assertEqual(signup.reviewed_by, self.physics_admin)
        self.assertFalse(User.objects.filter(username="newcomer").exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.SIGNUP_REJECT,
                user=self.physics_admin,
                document_department="Physics",
            ).exists()
        )

    def test_a_rejected_applicant_still_cannot_sign_in(self):
        signup = pending()
        self.reject(self.physics_admin, signup)

        response = self.login()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("sessionid", response.cookies)


class DepartmentRenameTests(ReviewTestCase):
    def test_a_rename_carries_waiting_requests_with_it(self):
        signup = pending(department="Physics")

        rename_department(Department.objects.get(name="Physics"), "Physics & Astronomy")

        signup.refresh_from_db()
        self.assertEqual(signup.department, "Physics & Astronomy")
        # Still in the renamed department's queue. A fresh instance: the
        # rename rewrote the admin's profile row under the cached one.
        admin = User.objects.get(pk=self.physics_admin.pk)
        listed = self.as_user(admin).get(reverse("admin-signup-request-list"))
        self.assertEqual([row["id"] for row in listed.data["results"]], [signup.id])
