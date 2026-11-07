"""The one way to tell someone something.

Other features call notify(); the producers in this app that coalesce
bursts call coalesce(). Both go through the same cleaning, so whatever a
caller passes, what is stored is plain text of bounded length and a link
that can only lead somewhere inside the app:

* Text is plain. The client renders it as text, never as HTML, so nothing
  here escapes markup - but control characters (a NUL is a database error
  in PostgreSQL) are removed, a title is one line, and everything is cut to
  its column with an ellipsis rather than raising on a long document title.
* A link is an in-app path ("/documents/12?tab=processing"). An absolute
  or protocol-relative URL, or a "javascript:" one, is refused outright:
  a notification is the last place an open redirect should live.

Nothing in here audits. A notification is a consequence of something that
was audited where it happened (an upload, an approval), and reading one is
not an action anyone needs a trail of.
"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Notification

TITLE_LIMIT = Notification._meta.get_field("title").max_length
BODY_LIMIT = Notification._meta.get_field("body").max_length
URL_LIMIT = Notification._meta.get_field("url").max_length
KIND_LIMIT = Notification._meta.get_field("kind").max_length
KEY_LIMIT = Notification._meta.get_field("dedupe_key").max_length
# Serialized. A payload is producer bookkeeping (ids, outcomes), not content.
PAYLOAD_LIMIT = 32_000

# A path inside this app: one leading slash, then no second slash or
# backslash straight after it (both make a protocol-relative URL that
# leaves the site), and no whitespace anywhere.
_IN_APP_PATH = re.compile(r"/(?![/\\])[^\s\\]*")
_KIND = re.compile(r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*")


@dataclass(frozen=True)
class Content:
    """What a notification says. `fresh` asks coalesce() to start a new
    notification even though an open one has the key - the open one is
    closed as it stands (a producer whose notification is full)."""

    kind: str
    title: str
    body: str = ""
    url: str = ""
    payload: dict | None = None
    fresh: bool = False


def notify(
    recipient,
    kind: str,
    title: str,
    *,
    body: str = "",
    url: str = "",
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> Notification:
    """Tell `recipient` something.

    With `dedupe_key`, an unread notification for the same recipient and key
    is updated in place - its text replaced, its time moved to now - instead
    of a second one being added. Without one, every call adds a row.

    Raises ValueError for a kind, link or payload that is not allowed; text
    that is merely too long is shortened.
    """
    content = Content(kind=kind, title=title, body=body, url=url, payload=payload)
    if not dedupe_key:
        return _create(recipient, "", clean(content), timezone.now())
    return coalesce(recipient, dedupe_key, lambda previous: content)


def coalesce(
    recipient,
    dedupe_key: str,
    build: Callable[[Notification | None], Content | None],
    *,
    window: timedelta | None = None,
) -> Notification | None:
    """Create or update the recipient's OPEN notification for `dedupe_key`.

    Open means unread and still holding the key - and, with a `window`,
    updated within it: older news is closed (it keeps its text) and the
    new news starts a fresh notification, so a batch uploaded this
    afternoon does not fold into one from last week that was never opened.

    `build(previous)` receives the open notification, locked, or None, and
    returns the Content to store - or None to leave everything as it is,
    which is how a producer stays idempotent. It runs inside the
    transaction, so it must not have side effects of its own.

    Returns the notification written, the open one untouched, or None.
    """
    key = _clean_key(dedupe_key)
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _coalesce_once(recipient, key, build, window)
        except IntegrityError:
            # Another process opened a notification with this key between
            # the look and the insert (the unique constraint caught it).
            # The second attempt finds theirs, waits on its lock, and
            # merges into it.
            if attempt:
                raise
    return None  # pragma: no cover - the loop returns or raises


def _coalesce_once(recipient, key, build, window):
    now = timezone.now()
    previous = (
        Notification.objects.select_for_update()
        .filter(recipient=recipient, dedupe_key=key, read_at__isnull=True)
        .first()
    )
    if (
        previous is not None
        and window is not None
        and previous.updated_at < now - window
    ):
        _close(previous)
        previous = None

    content = build(previous)
    if content is None:
        return previous
    content = clean(content)

    if previous is not None and content.fresh:
        _close(previous)
        previous = None
    if previous is None:
        return _create(recipient, key, content, now)

    previous.kind = content.kind
    previous.title = content.title
    previous.body = content.body
    previous.url = content.url
    previous.payload = content.payload
    previous.updated_at = now
    previous.save(
        update_fields=["kind", "title", "body", "url", "payload", "updated_at"]
    )
    return previous


def _create(recipient, key, content, now) -> Notification:
    return Notification.objects.create(
        recipient=recipient,
        kind=content.kind,
        title=content.title,
        body=content.body,
        url=content.url,
        payload=content.payload,
        dedupe_key=key,
        created_at=now,
        updated_at=now,
    )


def _close(notification: Notification) -> None:
    notification.dedupe_key = ""
    notification.save(update_fields=["dedupe_key"])


# --- Reading -------------------------------------------------------------------


def mark_read(recipient, notification_id: int) -> bool:
    """Mark one of `recipient`'s notifications read.

    Returns False when they have no notification with that id - someone
    else's is indistinguishable from none, which is what the API's 404 is.
    Reading one already read changes nothing (its read time stays the first).
    """
    mine = Notification.objects.filter(pk=notification_id, recipient=recipient)
    if not mine.exists():
        return False
    mine.filter(read_at__isnull=True).update(read_at=timezone.now())
    return True


def mark_all_read(recipient, *, until=None) -> int:
    """Mark every unread notification of `recipient` read; returns how many.

    With `until`, only those whose news is no newer than it: the client
    passes the newest time it has shown, so a notification that arrives
    between the person looking and the click is not marked read unseen.
    """
    unread = Notification.objects.filter(recipient=recipient, read_at__isnull=True)
    if until is not None:
        unread = unread.filter(updated_at__lte=until)
    return unread.update(read_at=timezone.now())


# --- Cleaning ------------------------------------------------------------------


def clean(content: Content) -> Content:
    kind = (content.kind or "").strip()
    if not kind or len(kind) > KIND_LIMIT or not _KIND.fullmatch(kind):
        raise ValueError(
            f"A notification kind is a short dotted lower-case name, not {kind!r}."
        )
    title = plain_text(content.title, TITLE_LIMIT, single_line=True)
    if not title:
        raise ValueError("A notification needs a title.")
    return Content(
        kind=kind,
        title=title,
        body=plain_text(content.body, BODY_LIMIT),
        url=_clean_url(content.url),
        payload=_clean_payload(content.payload),
        fresh=content.fresh,
    )


def plain_text(value, limit: int, *, single_line: bool = False) -> str:
    """Text with no control characters, trimmed, cut to `limit` with "…".

    Newlines survive in a body (the client shows them as line breaks) but
    not in a title. Runs of spaces collapse, so padding cannot push the
    words that matter past the cut.
    """
    text = str(value or "")
    kept = []
    for character in text:
        if character == "\n" and not single_line:
            kept.append("\n")
        elif character in "\t\r\n":
            kept.append(" ")
        elif unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            kept.append(character)
    lines = (" ".join(line.split()) for line in "".join(kept).split("\n"))
    text = "\n".join(line for line in lines if line)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _clean_url(url) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > URL_LIMIT or not _IN_APP_PATH.fullmatch(url):
        raise ValueError(
            f"A notification links to a path inside the app, such as "
            f"'/documents/12', not {url[:80]!r}."
        )
    return url


def _clean_payload(payload) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("A notification payload is a JSON object.")
    try:
        size = len(json.dumps(payload))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"A notification payload must be plain JSON: {exc}") from exc
    if size > PAYLOAD_LIMIT:
        raise ValueError(
            f"A notification payload is bookkeeping, at most {PAYLOAD_LIMIT} "
            f"bytes of JSON; this one is {size}."
        )
    return payload


def _clean_key(key) -> str:
    key = (key or "").strip()
    if not key or len(key) > KEY_LIMIT:
        raise ValueError(f"A dedupe key is 1-{KEY_LIMIT} characters.")
    return key
