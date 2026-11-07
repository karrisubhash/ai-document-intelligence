"""News about sign-up requests: for the administrators who can decide one,
and a welcome for the person it let in.

Driven by saves of SignupRequest (see receivers.py), not by accounts'
services or views, which are not this app's to edit. Every write that
matters is a save: the sign-up view creates the request, and
accounts.services.approve_signup / reject_signup - which the single, bulk
and Django-admin paths all go through - save the decided row.

* A new request notifies exactly the administrators who may review it,
  decided by accounts.permissions.reviewable_signups itself: every system
  administrator, and the administrators of the department the request
  names. Requests coalesce while unread - "3 people asked for an account" -
  so a class signing up together is one notification, not thirty.
* A decision settles those notifications: once every request an unread
  one mentions has been decided - by anyone - it is marked read, because
  nothing in it is waiting any more. Without that, the administrator who
  did not make the decision kept an unread "Nia asked for an account" for
  a request a colleague had approved an hour before.
* An approval welcomes the new account once, pointing at the dashboard;
  it is the first thing in their bell when they first sign in.

A declined applicant has no account, so there is nobody to notify in the
app.
"""

from functools import partial

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import SignupRequest, UserProfile
from accounts.permissions import reviewable_signups
from documents.authorization import get_profile

from . import service
from .models import Notification
from .processing import safely
from .service import Content

KEY = "signups.requested"
KIND_REQUESTED = "signup.requested"
KIND_WELCOME = "account.welcome"
REVIEW_URL = "/admin/users?tab=requests"
MAX_REQUESTS = 50
NAMED = 3

Role = UserProfile.Role
PENDING = SignupRequest.Status.PENDING


def signup_saved(sender, instance, created, **kwargs):
    """post_save receiver for SignupRequest; the work runs after the commit,
    for the reason given in processing.job_saved."""
    pk = instance.pk
    if created:
        if instance.status == PENDING:
            transaction.on_commit(lambda: safely(announce_request, pk))
    elif instance.status != PENDING:
        transaction.on_commit(lambda: safely(settle, pk))


def reviewers(signup) -> list:
    """The active administrators who may decide `signup`.

    The query only narrows the candidates - reviewable_signups, the rule
    the review screens use, makes the decision for each of them.
    """
    candidates = Q(is_superuser=True) | Q(profile__role=Role.SYSTEM_ADMIN)
    if signup.department:
        candidates |= Q(
            profile__role=Role.DEPARTMENT_ADMIN, profile__department=signup.department
        )
    people = (
        get_user_model()
        .objects.filter(candidates, is_active=True)
        .select_related("profile")
        .distinct()
        .order_by("pk")
    )
    this_request = SignupRequest.objects.filter(pk=signup.pk)
    return [
        person for person in people if reviewable_signups(person, this_request).exists()
    ]


def announce_request(pk: int) -> int:
    signup = SignupRequest.objects.filter(pk=pk, status=PENDING).first()
    if signup is None:
        return 0
    told = 0
    for person in reviewers(signup):
        written = service.coalesce(person, KEY, partial(merge, signup=signup))
        told += written is not None
    return told


def merge(previous, *, signup):
    payload = previous.payload if previous is not None else {}
    requests = [dict(entry) for entry in payload.get("requests", [])]
    if any(entry.get("id") == signup.pk for entry in requests):
        return None
    fresh = len(requests) >= MAX_REQUESTS
    if fresh:
        requests = []
    requests.append({"id": signup.pk, "name": applicant_name(signup)})

    if len(requests) == 1:
        title = f"{requests[0]['name']} asked for an account"
        body = _request_details(signup)
    else:
        title = f"{len(requests)} people asked for an account"
        body = f"From {_names(requests)}. Review them in Users."
    return Content(
        kind=KIND_REQUESTED,
        title=title,
        body=body,
        url=REVIEW_URL,
        payload={"requests": requests},
        fresh=fresh,
    )


def settle(pk: int) -> None:
    signup = SignupRequest.objects.select_related("user").filter(pk=pk).first()
    if signup is None or signup.status == PENDING:
        return
    resolve(signup)
    if signup.status == SignupRequest.Status.APPROVED and signup.user is not None:
        welcome(signup.user)


def resolve(signup) -> int:
    """Mark read the unread request notifications that wait on nothing now."""
    settled = 0
    mentioning = Notification.objects.filter(
        kind=KIND_REQUESTED,
        read_at__isnull=True,
        payload__contains={"requests": [{"id": signup.pk}]},
    )
    for notification in mentioning:
        ids = [entry.get("id") for entry in notification.payload.get("requests", [])]
        if SignupRequest.objects.filter(pk__in=ids, status=PENDING).exists():
            continue
        settled += Notification.objects.filter(
            pk=notification.pk, read_at__isnull=True
        ).update(read_at=timezone.now())
    return settled


def welcome(user) -> Notification | None:
    """The first notification an approved applicant sees. Once per account."""
    if not user.is_active:
        return None
    if Notification.objects.filter(recipient=user, kind=KIND_WELCOME).exists():
        return None
    profile = get_profile(user)
    role = Role(profile.role).label if profile.role in Role.values else ""
    lines = ["Your request for an account was approved."]
    if profile.department and role:
        lines.append(
            f"You have been added to {profile.department} with the {role} role."
        )
    elif role:
        lines.append(f"Your account has the {role} role.")
    lines.append(
        "Start from your dashboard: upload a document, or ask a question about "
        "the documents you can see."
    )
    name = user.first_name or user.get_username()
    return service.notify(
        user,
        KIND_WELCOME,
        f"Welcome to Document Intelligence, {name}",
        body="\n".join(lines),
        url="/",
        dedupe_key=KIND_WELCOME,
    )


def applicant_name(signup) -> str:
    full = " ".join(part for part in (signup.first_name, signup.last_name) if part)
    name = " ".join((full or signup.username).split())
    return name if len(name) <= 80 else name[:79].rstrip() + "…"


def _request_details(signup) -> str:
    role = Role(signup.role).label if signup.role in Role.values else signup.role
    if signup.department:
        return f"Asked to join {signup.department} with the {role} role. Review it in Users."
    return (
        f"Asked for the {role} role without naming a department; a system "
        f"administrator places them. Review it in Users."
    )


def _names(requests) -> str:
    named = [entry["name"] for entry in requests[:NAMED]]
    rest = len(requests) - len(named)
    if rest:
        return f"{', '.join(named)} and {rest} more"
    return f"{', '.join(named[:-1])} and {named[-1]}"
