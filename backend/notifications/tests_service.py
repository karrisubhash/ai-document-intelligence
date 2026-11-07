"""notifications.service: what every producer writes through."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from notifications import service
from notifications.models import Notification
from notifications.service import Content

User = get_user_model()


def person(username="nia"):
    return User.objects.create_user(username=username, password="pass1234")


class NotifyTests(TestCase):
    def setUp(self):
        self.nia = person()

    def test_it_stores_what_it_was_given(self):
        notification = service.notify(
            self.nia,
            "document.ready",
            "“Fee structure” is ready",
            body="Open it.",
            url="/documents/4",
            payload={"documents": [4]},
        )

        notification.refresh_from_db()
        self.assertEqual(notification.recipient, self.nia)
        self.assertEqual(notification.kind, "document.ready")
        self.assertEqual(notification.title, "“Fee structure” is ready")
        self.assertEqual(notification.body, "Open it.")
        self.assertEqual(notification.url, "/documents/4")
        self.assertEqual(notification.payload, {"documents": [4]})
        self.assertIsNone(notification.read_at)
        self.assertEqual(notification.created_at, notification.updated_at)

    def test_without_a_key_every_call_adds_one(self):
        service.notify(self.nia, "system.note", "One")
        service.notify(self.nia, "system.note", "One")

        self.assertEqual(Notification.objects.filter(recipient=self.nia).count(), 2)

    def test_an_unread_notification_with_the_key_is_updated_not_duplicated(self):
        first = service.notify(self.nia, "batch.done", "1 done", dedupe_key="batch")
        Notification.objects.filter(pk=first.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )

        second = service.notify(
            self.nia, "batch.done", "2 done", body="Two now.", dedupe_key="batch"
        )

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(Notification.objects.filter(recipient=self.nia).count(), 1)
        second.refresh_from_db()
        self.assertEqual((second.title, second.body), ("2 done", "Two now."))
        # Updated news is new news: it moves to the top of the feed.
        self.assertGreater(second.updated_at, second.created_at)

    def test_reading_it_closes_it_and_the_next_news_starts_another(self):
        first = service.notify(self.nia, "batch.done", "1 done", dedupe_key="batch")
        service.mark_read(self.nia, first.pk)

        second = service.notify(self.nia, "batch.done", "1 more", dedupe_key="batch")

        self.assertNotEqual(second.pk, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.title, "1 done")

    def test_keys_are_per_person(self):
        sam = person("sam")
        service.notify(self.nia, "batch.done", "Nia's", dedupe_key="batch")
        service.notify(sam, "batch.done", "Sam's", dedupe_key="batch")

        self.assertEqual(Notification.objects.get(recipient=self.nia).title, "Nia's")
        self.assertEqual(Notification.objects.get(recipient=sam).title, "Sam's")


class CoalesceTests(TestCase):
    def setUp(self):
        self.nia = person()

    def counting(self, previous):
        count = (previous.payload["count"] if previous else 0) + 1
        return Content(
            kind="batch.done", title=f"{count} done", payload={"count": count}
        )

    def test_build_sees_the_open_notification(self):
        service.coalesce(self.nia, "batch", self.counting)
        service.coalesce(self.nia, "batch", self.counting)
        notification = service.coalesce(self.nia, "batch", self.counting)

        self.assertEqual(notification.title, "3 done")
        self.assertEqual(Notification.objects.count(), 1)

    def test_none_from_build_changes_nothing(self):
        first = service.coalesce(self.nia, "batch", self.counting)
        stamp = first.updated_at

        self.assertIsNone(service.coalesce(self.nia, "batch", lambda previous: None))

        first.refresh_from_db()
        self.assertEqual((first.title, first.updated_at), ("1 done", stamp))
        self.assertIsNone(service.coalesce(person("sam"), "batch", lambda p: None))
        self.assertEqual(Notification.objects.count(), 1)

    def test_news_after_the_window_starts_a_new_notification(self):
        old = service.coalesce(self.nia, "batch", self.counting)
        Notification.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(minutes=11)
        )

        new = service.coalesce(
            self.nia, "batch", self.counting, window=timedelta(minutes=10)
        )

        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.title, "1 done")
        old.refresh_from_db()
        # Closed, not changed: still unread, keeping what it said.
        self.assertEqual((old.title, old.dedupe_key, old.read_at), ("1 done", "", None))

    def test_news_within_the_window_joins(self):
        old = service.coalesce(self.nia, "batch", self.counting)
        Notification.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(minutes=9)
        )

        joined = service.coalesce(
            self.nia, "batch", self.counting, window=timedelta(minutes=10)
        )

        self.assertEqual((joined.pk, joined.title), (old.pk, "2 done"))

    def test_fresh_closes_the_open_one(self):
        old = service.coalesce(self.nia, "batch", self.counting)

        new = service.coalesce(
            self.nia,
            "batch",
            lambda previous: Content(kind="batch.done", title="new", fresh=True),
        )

        self.assertNotEqual(new.pk, old.pk)
        old.refresh_from_db()
        self.assertEqual(old.dedupe_key, "")

    def test_the_database_allows_one_open_notification_per_person_and_key(self):
        def open_one(**fields):
            return Notification.objects.create(
                recipient=self.nia, kind="batch.done", title="t", **fields
            )

        open_one(dedupe_key="batch")
        # Read ones, and ones without a key, are not open.
        open_one(dedupe_key="batch", read_at=timezone.now())
        open_one()
        open_one()
        with self.assertRaises(IntegrityError), transaction.atomic():
            open_one(dedupe_key="batch")

    def test_an_insert_that_loses_a_race_merges_into_the_winner(self):
        # Another process opened a notification with the key after this one
        # looked and found none, so this insert hit the unique constraint.
        # The retry finds theirs and folds this news into it.
        Notification.objects.create(
            recipient=self.nia,
            kind="batch.done",
            title="1 done",
            payload={"count": 1},
            dedupe_key="batch",
        )
        real = service._coalesce_once
        attempts = []

        def lose_the_first_race(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise IntegrityError("unique_open_notification_key")
            return real(*args)

        with mock.patch.object(service, "_coalesce_once", lose_the_first_race):
            notification = service.coalesce(self.nia, "batch", self.counting)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(notification.title, "2 done")
        self.assertEqual(Notification.objects.filter(recipient=self.nia).count(), 1)

    def test_a_second_lost_race_is_an_error_not_a_loop(self):
        with (
            mock.patch.object(
                service, "_coalesce_once", side_effect=IntegrityError("again")
            ),
            self.assertRaises(IntegrityError),
        ):
            service.coalesce(self.nia, "batch", self.counting)


class CleaningTests(TestCase):
    def setUp(self):
        self.nia = person()

    def stored(self, **fields):
        fields.setdefault("kind", "system.note")
        fields.setdefault("title", "Title")
        kind = fields.pop("kind")
        title = fields.pop("title")
        return service.notify(self.nia, kind, title, **fields)

    def test_text_loses_control_characters_and_titles_stay_on_one_line(self):
        notification = self.stored(
            title="Fee\x00 structure\nis\tready‮",
            body="Line one\x07\n\n  Line   two  ",
        )

        self.assertEqual(notification.title, "Fee structure is ready")
        self.assertEqual(notification.body, "Line one\nLine two")

    def test_long_text_is_shortened_not_refused(self):
        notification = self.stored(title="x" * 500, body="y" * 5000)

        self.assertEqual(len(notification.title), 200)
        self.assertTrue(notification.title.endswith("…"))
        self.assertEqual(len(notification.body), 1000)

    def test_text_is_stored_as_text_not_markup(self):
        notification = self.stored(title="<img src=x onerror=alert(1)>")

        self.assertEqual(notification.title, "<img src=x onerror=alert(1)>")

    def test_a_title_is_required(self):
        with self.assertRaises(ValueError):
            self.stored(title=" \x00 ")

    def test_links_must_stay_inside_the_app(self):
        for url in (
            "https://evil.example/",
            "//evil.example/path",
            "/\\evil.example",
            "javascript:alert(1)",
            "documents/4",
            "/documents/4 extra",
            "/" + "a" * 600,
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.stored(url=url)

        self.assertEqual(
            self.stored(url="/documents/4?tab=processing").url,
            "/documents/4?tab=processing",
        )
        self.assertEqual(self.stored(url="").url, "")

    def test_kinds_are_short_dotted_names(self):
        for kind in ("", "Document Ready", "document..ready", "k" * 65, "doc/ready"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.stored(kind=kind)
        self.assertEqual(self.stored(kind="signup.requested").kind, "signup.requested")

    def test_payload_is_a_small_json_object(self):
        for payload in (["a"], {"when": timezone.now()}, {"x": "y" * 40_000}):
            with self.subTest(payload=type(payload)), self.assertRaises(ValueError):
                self.stored(payload=payload)
        self.assertEqual(self.stored(payload=None).payload, {})

    def test_a_dedupe_key_is_bounded(self):
        with self.assertRaises(ValueError):
            service.notify(self.nia, "system.note", "T", dedupe_key="k" * 201)


class ReadStateTests(TestCase):
    def setUp(self):
        self.nia = person()
        self.sam = person("sam")

    def test_mark_read_is_for_your_own_only(self):
        mine = service.notify(self.nia, "system.note", "Mine")

        self.assertFalse(service.mark_read(self.sam, mine.pk))
        mine.refresh_from_db()
        self.assertIsNone(mine.read_at)

        self.assertTrue(service.mark_read(self.nia, mine.pk))
        mine.refresh_from_db()
        first_read = mine.read_at
        self.assertIsNotNone(first_read)

        # Again: still true, and the first read time stands.
        self.assertTrue(service.mark_read(self.nia, mine.pk))
        mine.refresh_from_db()
        self.assertEqual(mine.read_at, first_read)

    def test_mark_all_read_counts_and_respects_until(self):
        older = service.notify(self.nia, "system.note", "Older")
        Notification.objects.filter(pk=older.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )
        service.notify(self.nia, "system.note", "Newer")
        service.notify(self.sam, "system.note", "Sam's")

        updated = service.mark_all_read(
            self.nia, until=timezone.now() - timedelta(minutes=1)
        )

        self.assertEqual(updated, 1)
        self.assertEqual(
            set(
                Notification.objects.filter(read_at__isnull=True).values_list(
                    "title", flat=True
                )
            ),
            {"Newer", "Sam's"},
        )
        self.assertEqual(service.mark_all_read(self.nia), 1)
        self.assertEqual(service.mark_all_read(self.nia), 0)
