from django.conf import settings
from django.db import models


class Department(models.Model):
    """The registry of departments - the tenant boundary, made explicit.

    Departments used to exist only as free text on UserProfile and Document,
    so the set of tenants was whatever anyone had ever typed. That made the
    boundary fragile in the most dangerous direction: an administrator who
    assigned "Computer Science " (trailing space) or "Comp Sci" created a new,
    empty tenant and quietly cut a user off from every colleague - and one who
    typed another department's exact name granted access to all of it.

    The registry is what assignments are now validated against. The name is
    still the value stored on profiles and documents, deliberately: every
    authorization query, search filter and evaluation dataset already keys on
    that string, and replacing it with a foreign key would rewrite the access
    rules to gain nothing the validation does not already give. Renaming a
    department rewrites every reference in one transaction - see
    accounts.services.rename_department.
    """

    name = models.CharField(max_length=255, unique=True)
    # Short form for listings and filters ("CSE"). Optional, unique when set.
    code = models.CharField(max_length=20, blank=True, default="")
    description = models.TextField(blank=True, default="")
    # An inactive department keeps its documents and members but accepts no
    # new assignments - retiring a department must not orphan its records.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["code"],
                condition=~models.Q(code=""),
                name="unique_department_code_when_set",
            )
        ]

    def __str__(self):
        return self.name


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
