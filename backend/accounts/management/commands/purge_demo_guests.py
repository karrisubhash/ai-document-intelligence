"""Delete demo guest accounts that have expired - or all of them.

    python manage.py purge_demo_guests            # older than DEMO_GUEST_TTL_HOURS
    python manage.py purge_demo_guests --dry-run  # count them, delete nothing
    python manage.py purge_demo_guests --all      # every guest, now

Schedule it - hourly is plenty. A new demo visitor also sweeps a few expired
guests on arrival, but only a few, and a quiet demo has no arrivals: this is
what bounds the guest accounts a deployment holds. Each guest goes with its
conversations, answer ratings, notifications and tokens; its audit rows stay,
with the account cleared, as for any deleted account. Nothing but accounts
flagged as guests is ever deleted (accounts.demo).

--all is for ending the demo now rather than a TTL later. With DEMO_ENABLED
off every guest's session already ends on its next request; this deletes the
accounts and what they made as well. With the demo still on, new visitors
can start one again straight away.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts import demo


class Command(BaseCommand):
    help = (
        "Delete demo guest accounts older than DEMO_GUEST_TTL_HOURS, or with "
        "--all every one of them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Delete every demo guest, whatever its age.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many guests would be deleted, delete nothing.",
        )

    def handle(self, *args, **options):
        hours = settings.DEMO_GUEST_TTL_HOURS
        which = "" if options["all"] else f" older than {hours} hours"
        if options["dry_run"]:
            guests = get_user_model().objects.filter(profile__is_guest=True)
            if not options["all"]:
                guests = guests.filter(
                    date_joined__lt=timezone.now() - demo.guest_ttl()
                )
            self.stdout.write(
                f"{guests.count()} demo guest account(s){which} would be deleted."
            )
            return

        if options["all"]:
            deleted = demo.purge_all_guests(trigger="command")
        else:
            deleted = demo.purge_expired_guests(trigger="command")
        remaining = demo.live_guests().count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} demo guest account(s){which}; {remaining} remain."
            )
        )
