"""Fill the demo department with the sample library.

    python manage.py seed_demo                      # DEMO_DEPARTMENT, made if missing
    python manage.py seed_demo --department Physics # any department, made if missing

Every sample goes through the real upload path and the whole pipeline (see
documents.samples), so the worker must be running for them to become Ready.
Safe to run again: samples the department already holds - by checksum - are
skipped. The demo is offered (DEMO_ENABLED=True) once at least one of them
has finished processing.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts import demo
from accounts.models import Department
from documents import samples


class Command(BaseCommand):
    help = "Load the sample library into the demo department (or another)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--department",
            default="",
            help="Department to fill; default DEMO_DEPARTMENT. Created if missing.",
        )

    def handle(self, *args, **options):
        name = options["department"].strip()
        try:
            if not name or name.lower() == settings.DEMO_DEPARTMENT.lower():
                department = demo.ensure_demo_department()
            else:
                department = Department.objects.filter(name__iexact=name).first()
                if department is None:
                    department = Department.objects.create(name=name)
                elif not department.is_active:
                    raise ValueError(f"{department.name!r} is inactive.")
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        try:
            outcome = samples.seed(department.name, actor=None, via="seed_demo")
        except samples.LibraryAccountTaken as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {len(outcome.queued)} sample document(s) for processing in "
                f"{department.name!r}; {len(outcome.skipped)} were already there."
            )
        )
        if outcome.queued:
            self.stdout.write(
                "They are Ready once the worker has processed them; watch with "
                "`docker compose logs -f worker`."
            )
