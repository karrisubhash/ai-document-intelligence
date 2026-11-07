from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Notification(models.Model):
    """One piece of news for one person: a finished upload, a sign-up to
    review, a welcome.

    Written only through notifications.service, which is what keeps the
    text plain and bounded and the link an in-app path. The text is stored
    as composed, not re-rendered on read: a notification says what was true
    when it was sent, which is what news is.

    `dedupe_key` is how a burst becomes one notification instead of many.
    While a notification is unread and keeps its key it is OPEN, and news
    with the same key updates it in place ("12 documents are ready")
    rather than adding a row. Reading it closes it - the next piece of news
    starts a new one - and so does the service, by clearing the key, when a
    producer's coalescing window has passed. The database enforces at most
    one open notification per person and key, so two workers finishing
    documents at the same moment cannot open two.
    """

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        # The feed index below leads with the recipient and serves every
        # lookup by it, deletion cascades included; a second index on the
        # column alone would only be written, never read.
        db_index=False,
    )
    # A dotted name the client picks an icon by ("document.ready"). Free
    # text rather than choices, so a new producer needs no migration.
    kind = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=1000, blank=True, default="")
    # An in-app path ("/documents/12"), never an absolute URL.
    url = models.CharField(max_length=500, blank=True, default="")
    # What the producer needs to coalesce and to stay idempotent (document
    # ids, job outcomes already announced). Never sent to the client.
    payload = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    # When the news in it last changed - set by the service, deliberately
    # not auto_now: marking a notification read is not news, and the
    # client watches the newest of these to tell when something arrived.
    updated_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            # The feed (newest first) and the summary's newest time.
            models.Index(
                fields=["recipient", "-updated_at", "-id"],
                name="notification_feed_idx",
            ),
            # The unread count, polled by every open tab, and read-all.
            models.Index(
                fields=["recipient"],
                condition=Q(read_at__isnull=True),
                name="notification_unread_idx",
            ),
            # Retention (prune_notifications) deletes by age.
            models.Index(fields=["updated_at"], name="notification_age_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=Q(read_at__isnull=True) & ~Q(dedupe_key=""),
                name="unique_open_notification_key",
            )
        ]

    def __str__(self):
        return f"{self.kind} for {self.recipient_id}: {self.title}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
