"""The one-click demo: ephemeral, read-only guest accounts.

A visitor who presses "Try the live demo" gets an account of their own -
not a shared "demo" login. A shared account would put every visitor's
questions and answers in one conversation list for the next visitor to
read, and one visitor changing its password or deleting its history would
break the demo for everyone. One account per visitor makes the existing
rules do the work: conversations are owner-scoped, so guests never see one
another's.

What makes such an account safe to hand to anyone:

* It has no password (an unusable one) and an unguessable name, so it can
  never be signed into again - not by its visitor, not by anyone else. Its
  only credential is the session cookie the demo endpoint sets.
* It cannot hold an API token (accounts.authentication refuses one).
* It is a STUDENT in the demo department and nothing else, so it reads
  exactly what that department holds - the sample library - and no
  administrator screen answers it.
* It is read-only: accounts.middleware.GuestReadOnlyMiddleware refuses
  every request that would change something, except asking questions,
  rating answers, marking notifications read and signing out.
* It expires. purge_expired_guests deletes guests older than
  DEMO_GUEST_TTL_HOURS with everything they made, and a few expired ones
  are swept each time a new guest arrives.
* It cannot hold the demo full. Starting one takes nothing but a CSRF
  cookie, so DEMO_MAX_GUESTS must never be a door that guests nobody uses
  can keep shut. At the ceiling a new visitor takes the place of the guest
  idle longest, once that is DEMO_GUEST_IDLE_MINUTES (create_guest), and
  the defaults keep THROTTLE_DEMO_GLOBAL x DEMO_GUEST_TTL_HOURS within the
  ceiling, so not even guests kept busy can fill it (accounts.checks warns
  about a configuration that can).

Guests are identified by UserProfile.is_guest, never by their name: the
"guest-" prefix only makes them recognisable in an audit trail, and sign-up
and account creation refuse it so no member can look like one. (The guard
reads the name only to wave members' reads through without a query -
named_like_guest; the flag is what decides.)
"""

import secrets
import string
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Department, UserProfile

GUEST_USERNAME_PREFIX = "guest-"
GUEST_FIRST_NAME = "Guest"
# 36^8 names: collisions are retried, and guessing one gains nothing - the
# account has no password to go with it.
_SUFFIX_LENGTH = 8
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits

# Expired guests deleted, at most, when a new one arrives - enough to keep
# up with arrivals between scheduled sweeps without making one visitor wait
# for a backlog. The management command has no such bound.
OPPORTUNISTIC_PURGE_LIMIT = 20
# Accounts deleted per transaction by a sweep, so a large backlog is not one
# long-held lock.
_PURGE_BATCH = 200
# How precisely a guest's last request is written down. Idleness is judged
# in tens of minutes (DEMO_GUEST_IDLE_MINUTES, at least 10), so a guest who
# is browsing costs one UPDATE every few minutes, not one per request.
SEEN_RESOLUTION = timedelta(minutes=5)

DEMO_DEPARTMENT_DESCRIPTION = (
    "Sample documents for the one-click demo. Demo visitors read and ask "
    "questions here; they cannot change anything."
)


class DemoUnavailable(Exception):
    """The demo is turned off, or has nothing to show yet."""


class DemoFull(Exception):
    """DEMO_MAX_GUESTS guest accounts already exist."""


def is_guest(user) -> bool:
    """Whether `user` is a demo guest. False for anyone not signed in."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    # A missing profile raises RelatedObjectDoesNotExist, an AttributeError.
    profile = getattr(user, "profile", None)
    return bool(getattr(profile, "is_guest", False))


def named_like_guest(user) -> bool:
    """Whether `user` could be a guest, judged without a query.

    Never False for a guest: every one is named "guest-..." (_new_guest), and
    a guest's name cannot be changed - not through the API, not in the Django
    admin (ProfileAwareUserAdmin). It can be True for a member
    - one made before the prefix was reserved, or by hand in the Django
    admin - so it only says whether to look closer. is_guest() decides.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return username_reserved(user.get_username())


def note_activity(user) -> None:
    """Write down that guest `user` has just made a request.

    At most once every SEEN_RESOLUTION: what is judged is idleness over tens
    of minutes, and a guest browsing should cost an UPDATE every few
    minutes, not one per request.
    """
    profile = user.profile
    now = timezone.now()
    seen = profile.last_seen_at
    if seen is not None and now - seen < SEEN_RESOLUTION:
        return
    UserProfile.objects.filter(pk=profile.pk).update(last_seen_at=now)
    profile.last_seen_at = now


def guest_ttl() -> timedelta:
    return timedelta(hours=settings.DEMO_GUEST_TTL_HOURS)


def idle_window() -> timedelta:
    return timedelta(minutes=settings.DEMO_GUEST_IDLE_MINUTES)


def session_seconds() -> int:
    """How long a guest's session lasts: the usual session lifetime, but
    never past the moment its account becomes due for deletion."""
    return max(60, min(settings.SESSION_COOKIE_AGE, int(guest_ttl().total_seconds())))


def demo_department() -> Department | None:
    """The demo department, if it exists and is active."""
    department = Department.objects.filter(
        name__iexact=settings.DEMO_DEPARTMENT.strip()
    ).first()
    if department is None or not department.is_active:
        return None
    return department


def ensure_demo_department() -> Department:
    """The demo department, created if it does not exist yet.

    For seeding (seed_demo). Raises ValueError for an inactive one: an
    administrator retired it, and filling it again is their decision.
    """
    name = settings.DEMO_DEPARTMENT.strip()
    department = Department.objects.filter(name__iexact=name).first()
    if department is None:
        # The short code only when it is free - codes are unique.
        code = "" if Department.objects.filter(code="DEMO").exists() else "DEMO"
        try:
            with transaction.atomic():
                department = Department.objects.create(
                    name=name, code=code, description=DEMO_DEPARTMENT_DESCRIPTION
                )
        except IntegrityError:
            # Created by a simultaneous run.
            department = Department.objects.get(name__iexact=name)
    if not department.is_active:
        raise ValueError(
            f"The demo department {department.name!r} is inactive. Reactivate it "
            f"in Departments before seeding it."
        )
    return department


def is_available() -> bool:
    """Whether to offer the demo: it is on, and its department holds at
    least one document ready to read.

    The second half is what keeps a misconfigured installation from
    handing visitors an empty workspace - DEMO_ENABLED turned on before
    `seed_demo` ran, or before its documents finished processing. The
    button appears once there is something to show.
    """
    if not settings.DEMO_ENABLED:
        return False
    department = demo_department()
    if department is None:
        return False

    from documents.models import Document

    return Document.objects.filter(
        department=department.name,
        status=Document.Status.COMPLETED,
        archived_at__isnull=True,
    ).exists()


def live_guests():
    return UserProfile.objects.filter(is_guest=True)


def create_guest():
    """A new guest account in the demo department, ready to be signed in.

    Raises DemoUnavailable when the demo is not on offer, and DemoFull when
    DEMO_MAX_GUESTS guests already exist and none of them can make way
    (_make_room).
    """
    if not is_available():
        raise DemoUnavailable()
    purge_expired_guests(limit=OPPORTUNISTIC_PURGE_LIMIT, trigger="demo")

    with transaction.atomic():
        # Locked, so simultaneous arrivals queue here and DEMO_MAX_GUESTS is
        # a real ceiling, not a count two visitors can both pass. Guests
        # arrive at most a few a minute (THROTTLE_DEMO_GLOBAL), so the queue
        # is short.
        current = demo_department()
        department = (
            Department.objects.select_for_update().filter(pk=current.pk).first()
            if current is not None
            else None
        )
        if department is None or not department.is_active:
            raise DemoUnavailable()
        over = live_guests().count() - settings.DEMO_MAX_GUESTS + 1
        if over <= 0 or _make_room(over) >= over:
            return _new_guest(department.name)
    # Raised outside the block, so whatever room was made is kept: those are
    # the same guests the next arrival would have to clear.
    raise DemoFull()


def _make_room(needed: int) -> int:
    """At the ceiling: delete up to `needed` guests nobody is using; how
    many went.

    Those idle for DEMO_GUEST_IDLE_MINUTES - counted from their last request
    (last_seen_at), or from the demo's start for one that never made any -
    and any past DEMO_GUEST_TTL_HOURS that a sweep has not reached yet. The
    longest idle go first, and never more than needed: an idle visitor may
    still come back, and below the ceiling nobody is displaced. So a guest
    started and abandoned - by a person, or by a script starting them in a
    loop - holds its place for minutes, not until it expires.
    """
    now = timezone.now()
    idle = Q(seen__lt=now - idle_window())
    expired = Q(date_joined__lt=now - guest_ttl())
    candidates = (
        get_user_model()
        .objects.filter(profile__is_guest=True)
        .alias(seen=Coalesce("profile__last_seen_at", "date_joined"))
        .filter(idle | expired)
        .order_by("seen", "pk")
    )
    deleted = _delete_guests(candidates, limit=needed)
    _record_purge(
        deleted,
        reason="idle",
        trigger="demo",
        idle_minutes=settings.DEMO_GUEST_IDLE_MINUTES,
    )
    return deleted


def _new_guest(department: str):
    User = get_user_model()
    for _ in range(5):
        suffix = "".join(
            secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LENGTH)
        )
        user = User(
            username=f"{GUEST_USERNAME_PREFIX}{suffix}", first_name=GUEST_FIRST_NAME
        )
        user.set_unusable_password()
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            continue
        # The post_save signal created the profile with defaults.
        profile = user.profile
        profile.role = UserProfile.Role.STUDENT
        profile.department = department
        profile.is_guest = True
        profile.save()
        return user
    raise RuntimeError("Could not find an unused guest name.")


def purge_expired_guests(*, limit: int | None = None, trigger: str = "command") -> int:
    """Delete guests older than DEMO_GUEST_TTL_HOURS; how many went.

    One GUEST_PURGE audit row per sweep that deleted anything, as for every
    way guests are deleted (_delete_guests).
    """
    expired = (
        get_user_model()
        .objects.filter(
            profile__is_guest=True, date_joined__lt=timezone.now() - guest_ttl()
        )
        .order_by("date_joined", "pk")
    )
    deleted = _delete_guests(expired, limit=limit)
    _record_purge(
        deleted,
        reason="expired",
        trigger=trigger,
        ttl_hours=settings.DEMO_GUEST_TTL_HOURS,
    )
    return deleted


def _delete_guests(candidates, *, limit: int | None = None) -> int:
    """Delete the accounts `candidates` lists, in its order and at most
    `limit` of them; how many went.

    Each takes with it what it owned: its conversations and their messages,
    its answer ratings, its notifications and any session token (all
    CASCADE). Its audit rows stay, with the account cleared, as for any
    deleted account - the trail is the institution's, not the visitor's.
    Only guests: the flag is checked again in the DELETE itself, so nothing
    else a query could ever match is removed. In batches, so a large backlog
    is not one long-held lock.
    """
    User = get_user_model()
    deleted = 0
    while limit is None or deleted < limit:
        size = _PURGE_BATCH if limit is None else min(_PURGE_BATCH, limit - deleted)
        ids = list(candidates.values_list("pk", flat=True)[:size])
        if not ids:
            break
        with transaction.atomic():
            _, per_model = User.objects.filter(
                pk__in=ids, profile__is_guest=True
            ).delete()
        removed = per_model.get(User._meta.label, 0)
        deleted += removed
        if removed == 0 or len(ids) < size:
            break
    return deleted


def _record_purge(deleted: int, **details) -> None:
    """One GUEST_PURGE audit row for a sweep that deleted anything: how many,
    which guests (`reason`: expired or idle) and which door asked
    (`trigger`: the command, or a demo start)."""
    if not deleted:
        return
    from documents.audit import record_event
    from documents.models import AuditLog

    record_event(
        user=None,
        action=AuditLog.Action.GUEST_PURGE,
        result=AuditLog.Result.ALLOWED,
        deleted=deleted,
        **details,
    )


def username_reserved(username: str) -> bool:
    """Whether a name is one only guests may have.

    Compared as a person reading a listing would: NFKC-normalised and
    case-folded, so neither "Guest-x" nor its full-width spelling passes.
    """
    folded = unicodedata.normalize("NFKC", username).strip().casefold()
    return folded.startswith(GUEST_USERNAME_PREFIX)
