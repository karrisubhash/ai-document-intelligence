"""Create the accounts and departments the end-to-end suite signs in as.

    python manage.py seed_e2e --password '<shared password>'

FOR DISPOSABLE DATABASES ONLY. It creates a system administrator, a
department administrator, a student and a faculty member in another
department - the minimum cast that exercises every permission boundary the
browser suite checks (frontend/e2e/platform.spec.ts). It refuses to run
against a database whose name does not say it is disposable, because
creating an administrator account with a known password in a real
institution's database is how test fixtures become incidents.

No documents are created: the suite uploads its own, from the governed
synthetic corpus in evaluation/corpus/, through the real upload path.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from accounts.models import Department, UserProfile

DISPOSABLE_MARKERS = ("e2e", "test", "ci")

DEPARTMENTS = [
    ("Computer Science", "CSE"),
    ("Mechanical Engineering", "MECH"),
    ("Physics", "PHY"),
]

PEOPLE = [
    ("admin", "Ada", "Admin", UserProfile.Role.SYSTEM_ADMIN, ""),
    (
        "cs_admin",
        "Chitra",
        "Sen",
        UserProfile.Role.DEPARTMENT_ADMIN,
        "Computer Science",
    ),
    ("asha", "Asha", "Rao", UserProfile.Role.STUDENT, "Computer Science"),
    ("ravi", "Ravi", "Kumar", UserProfile.Role.FACULTY, "Mechanical Engineering"),
]


class Command(BaseCommand):
    help = "Create end-to-end test accounts in a DISPOSABLE database."

    def add_arguments(self, parser):
        parser.add_argument("--password", required=True)

    def handle(self, *args, **options):
        name = connection.settings_dict.get("NAME") or ""
        if not any(marker in name.lower() for marker in DISPOSABLE_MARKERS):
            raise CommandError(
                f"Refusing to seed test accounts into database {name!r}. This "
                f"command only runs against a database whose name contains one "
                f"of {DISPOSABLE_MARKERS}, e.g. POSTGRES_DB=adi_e2e."
            )

        password = options["password"]
        if len(password) < 12:
            raise CommandError("Use a password of at least 12 characters.")

        for department, code in DEPARTMENTS:
            Department.objects.get_or_create(name=department, defaults={"code": code})

        User = get_user_model()
        for username, first, last, role, department in PEOPLE:
            user, _ = User.objects.get_or_create(
                username=username, defaults={"first_name": first, "last_name": last}
            )
            user.set_password(password)
            user.save()
            user.profile.role = role
            user.profile.department = department
            user.profile.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(PEOPLE)} accounts and {len(DEPARTMENTS)} departments "
                f"into {name}."
            )
        )
