from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


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
    # A one-click demo visitor (accounts.demo): anonymous, read-only and
    # short-lived. Only demo.create_guest sets it - no form or API field
    # does - and accounts.middleware.GuestReadOnlyMiddleware enforces what
    # it means on every request, whatever the browser sends.
    #
    # db_default as well as default: while an upgrade is under way the
    # previous release keeps serving after `migrate` commits, and its
    # INSERTs (every new account makes a profile) do not name this column.
    is_guest = models.BooleanField(default=False, db_default=False)
    # When a demo guest last made a request, to within a few minutes
    # (accounts.demo.note_activity): at DEMO_MAX_GUESTS, the guest idle
    # longest makes way for a new visitor. Null for a guest that has made no
    # request since its demo started - and for every member, whose requests
    # are never noted.
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Guests are counted on every demo start (DEMO_MAX_GUESTS) and
            # swept by age; a partial index keeps both off the member rows.
            models.Index(
                fields=["is_guest"],
                condition=models.Q(is_guest=True),
                name="profile_guest_idx",
            )
        ]

    def __str__(self):
        return f"{self.user.get_username()} ({self.role})"


class SignupRequest(models.Model):
    """Someone asking for an account, until an administrator decides.

    Deliberately not an inactive User. An account that exists but is
    switched off is one setting away from signing in - through the Django
    admin, a shell, or the edit dialog's "Active" box ticked by an
    administrator who took it for a deactivated colleague - and it would sit
    in every user listing and department head count while it waited. A
    request becomes an account only through approval (accounts.services.
    approve_signup), which is also where an administrator picks the role and
    department it gets.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    username = models.CharField(max_length=150)
    email = models.EmailField()
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150, blank=True, default="")
    # A password hash (make_password), never the password. Kept while the
    # request is pending, so approval can hand it to the new account and the
    # applicant signs in with the password they chose; and after a
    # rejection, so the sign-in screen can tell them their request was
    # declined rather than that their password is wrong. Cleared once an
    # account holds it.
    password = models.CharField(max_length=128, blank=True, default="")
    # What the applicant asked for. The administrator decides what the
    # account actually gets, and approval validates that decision.
    department = models.CharField(max_length=255, blank=True, default="")
    role = models.CharField(
        max_length=30,
        choices=UserProfile.Role.choices,
        default=UserProfile.Role.STUDENT,
    )
    note = models.CharField(max_length=500, blank=True, default="")

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Shown to the applicant when they next try to sign in.
    decision_note = models.CharField(max_length=500, blank=True, default="")
    # The account approval created.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signup_request",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One open request per username, whatever its case. The
            # serializer checks too, but two simultaneous sign-ups for
            # "alice" and "Alice" would both pass that check, and approving
            # one would then fail the other for a reason neither applicant
            # could see.
            models.UniqueConstraint(
                Lower("username"),
                condition=models.Q(status="PENDING"),
                name="unique_pending_signup_username",
            )
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="signup_status_idx")
        ]

    def __str__(self):
        return f"{self.username} ({self.status})"
