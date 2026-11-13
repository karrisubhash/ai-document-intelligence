"""Delete demo guest accounts that have expired.

    python manage.py purge_demo_guests            # older than DEMO_GUEST_TTL_HOURS
    python manage.py purge_demo_guests --dry-run  # count them, delete nothing

Schedule it - hourly is plenty. A new demo visitor also sweeps a few expired
guests on arrival, but only a few, and a quiet demo has no arrivals: this is
what bounds the guest accounts a deployment holds. Each guest goes with its
conversations, answer ratings, notifications and tokens; its audit rows stay,
with the account cleared, as for any deleted account. Nothing but accounts
flagged as guests is ever deleted (accounts.demo.purge_expired_guests).
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts import demo


class Command(BaseCommand):
    help = "Delete demo guest accounts older than DEMO_GUEST_TTL_HOURS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many guests have expired, delete nothing.",
        )

    def handle(self, *args, **options):
        hours = settings.DEMO_GUEST_TTL_HOURS
        if options["dry_run"]:
            expired = (
                get_user_model()
                .objects.filter(
                    profile__is_guest=True,
                    date_joined__lt=timezone.now() - demo.guest_ttl(),
                )
                .count()
            )
            self.stdout.write(
                f"{expired} demo guest account(s) older than {hours} hours would be "
                f"deleted."
            )
            return

        deleted = demo.purge_expired_guests(trigger="command")
        remaining = demo.live_guests().count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} demo guest account(s) older than {hours} hours; "
                f"{remaining} remain."
            )
        )
