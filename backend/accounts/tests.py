from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile

User = get_user_model()


class UserProfileTests(APITestCase):
    def test_profile_is_created_for_new_user(self):
        user = User.objects.create_user(username="student1", password="pass1234")

        self.assertEqual(user.profile.role, UserProfile.Role.STUDENT)
        self.assertEqual(user.profile.department, "")

    def test_superuser_gets_system_admin_role(self):
        user = User.objects.create_superuser(
            username="root", email="root@example.com", password="pass1234"
        )

        self.assertEqual(user.profile.role, UserProfile.Role.SYSTEM_ADMIN)


class MeEndpointTests(APITestCase):
    def test_me_requires_authentication(self):
        response = self.client.get(reverse("me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_role_and_department(self):
        user = User.objects.create_user(username="cs_faculty", password="pass1234")
        user.profile.role = UserProfile.Role.FACULTY
        user.profile.department = "Computer Science"
        user.profile.save()

        self.client.force_authenticate(user=user)
        response = self.client.get(reverse("me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "cs_faculty")
        self.assertEqual(response.data["role"], UserProfile.Role.FACULTY)
        self.assertEqual(response.data["department"], "Computer Science")
