"""Sign-up requests reach the administrators who can decide them; approval
welcomes the new account; decisions settle what was waiting."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Department, SignupRequest, UserProfile
from accounts.services import approve_signup
from notifications import signups
from notifications.models import Notification

User = get_user_model()
Role = UserProfile.Role
STRONG = "a-sufficiently-long-passphrase-9"


def make_user(username, role=Role.FACULTY, department="Physics", **extra):
    user = User.objects.create_user(username=username, password=STRONG, **extra)
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


class SignupNotificationTestCase(TestCase):
    def setUp(self):
        Department.objects.create(name="Physics", code="PHY")
        Department.objects.create(name="Chemistry", code="CHE")
        self.system_admin = make_user("root", Role.SYSTEM_ADMIN, "")
        self.superuser = User.objects.create_superuser(
            "super", "super@example.edu", STRONG
        )
        self.physics_admin = make_user("phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        self.chemistry_admin = make_user(
            "che_admin", Role.DEPARTMENT_ADMIN, "Chemistry"
        )
        self.departmentless_admin = make_user("lost_admin", Role.DEPARTMENT_ADMIN, "")
        self.physics_student = make_user("phy_student", Role.STUDENT, "Physics")
        self.retired_admin = make_user("retired", Role.SYSTEM_ADMIN, "")
        self.retired_admin.is_active = False
        self.retired_admin.save()

    def apply(self, **overrides):
        with self.captureOnCommitCallbacks(execute=True):
            response = APIClient().post(
                reverse("auth-signup"), application(**overrides), format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return SignupRequest.objects.get(username=overrides.get("username", "newcomer"))

    def decide(self, actor, signup, decision="approve"):
        client = APIClient()
        client.force_authenticate(actor)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse(f"admin-signup-request-{decision}", args=[signup.id]),
                {},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def told(self, user):
        return list(Notification.objects.filter(recipient=user).order_by("pk"))


class RequestedTests(SignupNotificationTestCase):
    def test_a_request_tells_exactly_the_administrators_who_can_decide_it(self):
        self.apply()

        notified = set(
            Notification.objects.values_list("recipient__username", flat=True)
        )
        self.assertEqual(notified, {"root", "super", "phy_admin"})

        [notification] = self.told(self.physics_admin)
        self.assertEqual(notification.kind, "signup.requested")
        self.assertEqual(notification.title, "Nia Okafor asked for an account")
        self.assertEqual(notification.url, "/admin/users?tab=requests")
        self.assertIn("Physics", notification.body)
        self.assertIn("Student", notification.body)
        # Their note and address stay on the review screen.
        self.assertNotIn("First-year", notification.body)
        self.assertNotIn("@", notification.body)

    def test_a_request_naming_no_department_is_for_system_administrators(self):
        self.apply(department="")

        notified = set(
            Notification.objects.values_list("recipient__username", flat=True)
        )
        self.assertEqual(notified, {"root", "super"})
        self.assertIn("without naming a department", self.told(self.superuser)[0].body)

    def test_requests_coalesce_per_administrator_while_unread(self):
        self.apply()
        self.apply(username="sam", first_name="Sam", last_name="Lee")
        self.apply(
            username="ali", first_name="Ali", last_name="", department="Chemistry"
        )

        [physics] = self.told(self.physics_admin)
        self.assertEqual(physics.title, "2 people asked for an account")
        self.assertEqual(
            physics.body, "From Nia Okafor and Sam Lee. Review them in Users."
        )
        [everyone] = self.told(self.system_admin)
        self.assertEqual(everyone.title, "3 people asked for an account")
        self.assertEqual(
            everyone.body, "From Nia Okafor, Sam Lee and Ali. Review them in Users."
        )
        [chemistry] = self.told(self.chemistry_admin)
        self.assertEqual(chemistry.title, "Ali asked for an account")

    def test_after_reading_the_next_request_is_new_news(self):
        self.apply()
        Notification.objects.update(read_at=timezone.now())

        self.apply(username="sam", first_name="Sam", last_name="Lee")

        titles = [n.title for n in self.told(self.physics_admin)]
        self.assertEqual(
            titles, ["Nia Okafor asked for an account", "Sam Lee asked for an account"]
        )

    def test_announcing_the_same_request_again_changes_nothing(self):
        signup = self.apply()

        self.assertEqual(signups.announce_request(signup.pk), 0)

        self.assertEqual(len(self.told(self.system_admin)), 1)
        self.assertEqual(
            self.told(self.system_admin)[0].title, "Nia Okafor asked for an account"
        )


class DecisionTests(SignupNotificationTestCase):
    def test_approval_welcomes_the_new_account_once(self):
        signup = self.apply()

        self.decide(self.physics_admin, signup)

        user = User.objects.get(username="newcomer")
        [welcome] = self.told(user)
        self.assertEqual(welcome.kind, "account.welcome")
        self.assertEqual(welcome.title, "Welcome to Document Intelligence, Nia")
        self.assertEqual(welcome.url, "/")
        self.assertIn(
            "You have been added to Physics with the Student role.", welcome.body
        )
        self.assertIsNone(welcome.read_at)

        signup.refresh_from_db()
        signups.settle(signup.pk)
        self.assertEqual(len(self.told(user)), 1)

    def test_an_approval_outside_the_api_welcomes_too(self):
        signup = self.apply()

        with self.captureOnCommitCallbacks(execute=True):
            approve_signup(
                signup,
                reviewer=self.system_admin,
                role=Role.FACULTY,
                department="Chemistry",
            )

        [welcome] = self.told(User.objects.get(username="newcomer"))
        self.assertIn("Chemistry with the Faculty role", welcome.body)

    def test_a_decision_settles_every_administrators_notification(self):
        signup = self.apply()

        self.decide(self.physics_admin, signup)

        for admin in (self.system_admin, self.superuser, self.physics_admin):
            [notification] = self.told(admin)
            self.assertIsNotNone(notification.read_at, admin.username)

    def test_a_notification_still_waiting_on_someone_stays_unread(self):
        nia = self.apply()
        sam = self.apply(username="sam", first_name="Sam", last_name="Lee")

        self.decide(self.physics_admin, nia)
        [notification] = self.told(self.physics_admin)
        self.assertIsNone(notification.read_at)

        self.decide(self.physics_admin, sam, "reject")
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)

    def test_a_declined_applicant_has_no_account_to_notify(self):
        signup = self.apply()
        before = Notification.objects.count()

        self.decide(self.system_admin, signup, "reject")

        self.assertEqual(Notification.objects.count(), before)
        self.assertFalse(
            Notification.objects.filter(read_at__isnull=True).exists(),
            "the administrators' notifications are settled",
        )

    def test_a_bulk_decline_settles_too(self):
        nia = self.apply()
        sam = self.apply(username="sam", first_name="Sam", last_name="Lee")
        client = APIClient()
        client.force_authenticate(self.system_admin)

        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(
                reverse("admin-signup-request-bulk-reject"),
                {"ids": [nia.pk, sam.pk]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(Notification.objects.filter(read_at__isnull=True).exists())

    def test_an_inactive_account_gets_no_welcome(self):
        signup = self.apply()
        self.decide(self.physics_admin, signup)
        user = User.objects.get(username="newcomer")
        Notification.objects.filter(recipient=user).delete()
        user.is_active = False
        user.save()

        self.assertIsNone(signups.welcome(user))
