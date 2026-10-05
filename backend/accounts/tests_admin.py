"""Administration: who may manage whom, and the department registry."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import Department, UserProfile
from documents.models import AuditLog, Document

User = get_user_model()
Role = UserProfile.Role

STRONG = "a-sufficiently-long-passphrase-9"


def make_user(username, role=Role.FACULTY, department="Physics"):
    user = User.objects.create_user(username=username, password=STRONG)
    user.profile.role = role
    user.profile.department = department
    user.profile.save()
    return user


class AdminTestCase(TestCase):
    def setUp(self):
        Department.objects.create(name="Physics", code="PHY")
        Department.objects.create(name="Chemistry", code="CHE")
        self.system_admin = make_user("root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user("phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        self.physics_student = make_user("phy_student", Role.STUDENT, "Physics")
        self.chemistry_student = make_user("che_student", Role.STUDENT, "Chemistry")

    def as_user(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client


class UserAdministrationTests(AdminTestCase):
    def test_an_ordinary_user_cannot_reach_administration(self):
        client = self.as_user(self.physics_student)
        for route in ("admin-user-list", "admin-department-list", "admin-audit"):
            with self.subTest(route=route):
                self.assertEqual(
                    client.get(reverse(route)).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_a_department_admin_sees_only_their_department(self):
        response = self.as_user(self.physics_admin).get(reverse("admin-user-list"))
        usernames = {row["username"] for row in response.data["results"]}

        self.assertIn("phy_student", usernames)
        self.assertNotIn("che_student", usernames)
        self.assertNotIn("root", usernames)

    def test_another_departments_account_is_invisible_not_forbidden(self):
        response = self.as_user(self.physics_admin).get(
            reverse("admin-user-detail", args=[self.chemistry_student.id])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_department_admin_creates_accounts_in_their_own_department(self):
        response = self.as_user(self.physics_admin).post(
            reverse("admin-user-list"),
            {"username": "new_student", "password": STRONG, "role": Role.STUDENT},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        created = User.objects.get(username="new_student")
        self.assertEqual(created.profile.department, "Physics")
        self.assertTrue(created.check_password(STRONG))
        self.assertNotIn("password", response.data)
        self.assertTrue(
            AuditLog.objects.filter(
                user=self.physics_admin, action=AuditLog.Action.USER_CREATE
            ).exists()
        )

    def test_a_department_admin_cannot_mint_an_admin(self):
        """Otherwise they could escalate themselves through a second account."""
        for role in (Role.DEPARTMENT_ADMIN, Role.SYSTEM_ADMIN):
            with self.subTest(role=role):
                response = self.as_user(self.physics_admin).post(
                    reverse("admin-user-list"),
                    {"username": f"x_{role}", "password": STRONG, "role": role},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("role", response.data)

    def test_a_department_admin_cannot_place_anyone_elsewhere(self):
        response = self.as_user(self.physics_admin).patch(
            reverse("admin-user-detail", args=[self.physics_student.id]),
            {"department": "Chemistry"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.physics_student.refresh_from_db()
        self.assertEqual(self.physics_student.profile.department, "Physics")

    def test_a_department_admin_cannot_edit_another_admin(self):
        peer = make_user("phy_admin_2", Role.DEPARTMENT_ADMIN, "Physics")
        response = self.as_user(self.physics_admin).patch(
            reverse("admin-user-detail", args=[peer.id]),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_nobody_can_demote_or_deactivate_themselves(self):
        client = self.as_user(self.system_admin)
        url = reverse("admin-user-detail", args=[self.system_admin.id])

        demote = client.patch(url, {"role": Role.STUDENT}, format="json")
        deactivate = client.patch(url, {"is_active": False}, format="json")

        self.assertEqual(demote.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(deactivate.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deactivation_revokes_tokens_and_records_what_changed(self):
        Token.objects.create(user=self.physics_student)
        response = self.as_user(self.physics_admin).patch(
            reverse("admin-user-detail", args=[self.physics_student.id]),
            {"is_active": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(Token.objects.filter(user=self.physics_student).exists())
        entry = AuditLog.objects.filter(action=AuditLog.Action.USER_UPDATE).latest("id")
        self.assertEqual(entry.metadata["changed"]["is_active"], [True, False])

    def test_departments_are_validated_and_normalized(self):
        client = self.as_user(self.system_admin)
        url = reverse("admin-user-detail", args=[self.chemistry_student.id])

        typo = client.patch(url, {"department": "Chemistr"}, format="json")
        casing = client.patch(url, {"department": "  physics "}, format="json")

        self.assertEqual(typo.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(casing.status_code, status.HTTP_200_OK)
        self.chemistry_student.refresh_from_db()
        self.assertEqual(self.chemistry_student.profile.department, "Physics")

    def test_an_inactive_department_accepts_no_new_members(self):
        Department.objects.filter(name="Chemistry").update(is_active=False)
        response = self.as_user(self.system_admin).patch(
            reverse("admin-user-detail", args=[self.physics_student.id]),
            {"department": "Chemistry"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_usernames_are_immutable(self):
        response = self.as_user(self.system_admin).patch(
            reverse("admin-user-detail", args=[self.physics_student.id]),
            {"username": "renamed"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_password_revokes_tokens_and_rejects_weak_passwords(self):
        Token.objects.create(user=self.physics_student)
        client = self.as_user(self.physics_admin)
        url = reverse("admin-user-set-password", args=[self.physics_student.id])

        weak = client.post(url, {"password": "123"}, format="json")
        strong = client.post(url, {"password": STRONG + "x"}, format="json")

        self.assertEqual(weak.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(strong.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Token.objects.filter(user=self.physics_student).exists())

    def test_the_role_picker_offers_only_assignable_roles(self):
        roles = {
            row["value"]
            for row in self.as_user(self.physics_admin)
            .get(reverse("admin-user-roles"))
            .data
        }
        self.assertEqual(roles, {Role.FACULTY, Role.STAFF, Role.STUDENT})


class DepartmentRegistryTests(AdminTestCase):
    def test_a_department_admin_may_read_but_not_write_the_registry(self):
        client = self.as_user(self.physics_admin)
        self.assertEqual(client.get(reverse("admin-department-list")).status_code, 200)
        self.assertEqual(
            client.post(
                reverse("admin-department-list"), {"name": "Biology"}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_names_are_unique_regardless_of_case(self):
        response = self.as_user(self.system_admin).post(
            reverse("admin-department-list"), {"name": "physics"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_counts_are_reported(self):
        Document.objects.create(title="Lab Rules", department="Physics")
        response = self.as_user(self.system_admin).get(reverse("admin-department-list"))
        physics = next(r for r in response.data["results"] if r["name"] == "Physics")

        self.assertEqual(physics["member_count"], 2)
        self.assertEqual(physics["document_count"], 1)

    def test_a_rename_carries_every_member_and_document_with_it(self):
        document = Document.objects.create(title="Lab Rules", department="Physics")
        physics = Department.objects.get(name="Physics")

        response = self.as_user(self.system_admin).patch(
            reverse("admin-department-detail", args=[physics.id]),
            {"name": "Physical Sciences"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        document.refresh_from_db()
        self.physics_student.refresh_from_db()
        self.assertEqual(document.department, "Physical Sciences")
        self.assertEqual(self.physics_student.profile.department, "Physical Sciences")
        # The boundary survives the rename: the student still sees the document.
        from documents import authorization

        self.assertTrue(authorization.can_view_document(self.physics_student, document))

    def test_the_public_list_shows_only_active_departments(self):
        Department.objects.filter(name="Chemistry").update(is_active=False)
        names = [
            row["name"]
            for row in self.as_user(self.physics_student)
            .get(reverse("department-list"))
            .data
        ]
        self.assertEqual(names, ["Physics"])


class PrivilegeBoundaryTests(AdminTestCase):
    """Nobody takes over an account holding more power than they have."""

    def set_password(self, actor, target):
        return self.as_user(actor).post(
            reverse("admin-user-set-password", args=[target.id]),
            {"password": STRONG + "-new"},
            format="json",
        )

    def test_a_role_only_system_admin_cannot_reset_a_superusers_password(self):
        superuser = User.objects.create_superuser("owner", password=STRONG)

        response = self.set_password(self.system_admin, superuser)

        self.assertNotEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        superuser.refresh_from_db()
        self.assertTrue(superuser.check_password(STRONG))

    def test_a_department_admin_cannot_reset_a_staff_account(self):
        staff = make_user("phy_staff", Role.STAFF, "Physics")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])

        response = self.set_password(self.physics_admin, staff)

        self.assertNotEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        staff.refresh_from_db()
        self.assertTrue(staff.check_password(STRONG))

    def test_a_superuser_may_still_manage_another_superuser(self):
        actor = User.objects.create_superuser("owner", password=STRONG)
        target = User.objects.create_superuser("owner2", password=STRONG)

        self.assertEqual(
            self.set_password(actor, target).status_code, status.HTTP_204_NO_CONTENT
        )


class DepartmentDisclosureTests(AdminTestCase):
    def test_a_department_admin_sees_only_their_own_departments_counts(self):
        rows = {
            row["name"]: row
            for row in self.as_user(self.physics_admin)
            .get(reverse("admin-department-list"))
            .data["results"]
        }

        self.assertIsNotNone(rows["Physics"]["member_count"])
        self.assertIsNone(rows["Chemistry"]["member_count"])
        self.assertIsNone(rows["Chemistry"]["document_count"])

    def test_a_system_admin_sees_every_count(self):
        rows = (
            self.as_user(self.system_admin)
            .get(reverse("admin-department-list"))
            .data["results"]
        )
        self.assertTrue(all(row["member_count"] is not None for row in rows))

    def test_an_inactive_department_takes_no_new_members(self):
        Department.objects.filter(name="Physics").update(is_active=False)

        response = self.as_user(self.physics_admin).post(
            reverse("admin-user-list"),
            {"username": "late_joiner", "password": STRONG, "role": Role.STUDENT},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(username="late_joiner").exists())

    def test_a_rename_onto_an_unregistered_spelling_in_use_is_refused(self):
        """ "physics" records are a separate tenant; renaming Physics onto
        that spelling would merge them."""
        make_user("lowercase_member", Role.STUDENT, "physics")
        physics = Department.objects.get(name="Physics")

        response = self.as_user(self.system_admin).patch(
            reverse("admin-department-detail", args=[physics.id]),
            {"name": "physics"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        physics.refresh_from_db()
        self.assertEqual(physics.name, "Physics")
        self.assertEqual(
            UserProfile.objects.filter(department="Physics").count(),
            2,  # phy_admin and phy_student, untouched
        )
