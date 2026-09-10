from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    class Role(models.TextChoices):
        SYSTEM_ADMIN = "SYSTEM_ADMIN", "System Admin"
        DEPARTMENT_ADMIN = "DEPARTMENT_ADMIN", "Department Admin"
        FACULTY = "FACULTY", "Faculty"
        STAFF = "STAFF", "Staff"
        STUDENT = "STUDENT", "Student"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField(
        max_length=30,
        choices=Role.choices,
        default=Role.STUDENT,
    )
    department = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.user.get_username()} ({self.role})"
