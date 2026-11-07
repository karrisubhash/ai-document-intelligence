"""Delete old notifications.

Read ones older than NOTIFICATIONS_RETENTION_DAYS (default 30): a read
notification has done its job, and keeping them forever only slows the feed
query every open tab polls. Unread ones older than
NOTIFICATIONS_UNREAD_RETENTION_DAYS (default 180), so the table cannot grow
without bound for an account nobody signs in to. Age is the time of the
latest news in a notification (`updated_at`), not when it was read.

Run from cron, like recover_stalled_jobs - there is no beat service:

    python manage.py prune_notifications             # delete
    python manage.py prune_notifications --dry-run   # count only
    python manage.py prune_notifications --read-days 7 --unread-days 0

0 days for either keeps that kind forever.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from notifications.models import Notification

BATCH = 5000


class Command(BaseCommand):
    help = "Delete read notifications (and very old unread ones) past retention."

    def add_arguments(self, parser):
        parser.add_argument(
            "--read-days",
            type=int,
            default=None,
            help="Delete read notifications older than this (default: setting).",
        )
        parser.add_argument(
            "--unread-days",
            type=int,
            default=None,
            help="Delete unread notifications older than this (default: setting).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would go; delete nothing.",
        )

    def handle(self, *args, **options):
        read_days = options["read_days"]
        if read_days is None:
            read_days = getattr(settings, "NOTIFICATIONS_RETENTION_DAYS", 30)
        unread_days = options["unread_days"]
        if unread_days is None:
            unread_days = getattr(settings, "NOTIFICATIONS_UNREAD_RETENTION_DAYS", 180)
        if read_days < 0 or unread_days < 0:
            raise CommandError("Retention is a number of days, 0 or more.")

        now = timezone.now()
        targets = []
        if read_days:
            targets.append(
                (
                    "read",
                    Notification.objects.filter(
                        read_at__isnull=False,
                        updated_at__lt=now - timedelta(days=read_days),
                    ),
                )
            )
        if unread_days:
            targets.append(
                (
                    "unread",
                    Notification.objects.filter(
                        read_at__isnull=True,
                        updated_at__lt=now - timedelta(days=unread_days),
                    ),
                )
            )

        for label, queryset in targets:
            if options["dry_run"]:
                self.stdout.write(
                    f"{queryset.count()} {label} notification(s) would be deleted."
                )
                continue
            deleted = 0
            # In batches, so a first run over a large backlog never holds
            # one long delete against the table every tab is polling.
            while True:
                ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:BATCH])
                if not ids:
                    break
                deleted += Notification.objects.filter(pk__in=ids).delete()[0]
            self.stdout.write(
                self.style.SUCCESS(f"{deleted} {label} notification(s) deleted.")
            )
