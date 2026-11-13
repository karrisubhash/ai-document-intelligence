"""Demo guests read, search and ask - and change nothing. Enforced here, once.

Every request that is not a read (anything but GET, HEAD and OPTIONS) from
a demo guest's session is refused with a 403, unless the view it resolves
to is on the allowlist below. One place, ahead of every view, rather than a
check in each of them: the views are shared with members and know nothing
about guests, an endpoint added next month is closed to guests without
anyone remembering to close it, and what a guest may do can be read off
one list.

Why a middleware and not a DRF permission class: a view that declares its
own permission_classes - most of them do - replaces the default list, so a
global permission would not have applied to them. A middleware sees every
request, DRF or not.

Why the session is enough to look at: a guest has no password (so no
token can be issued to it) and accounts.authentication refuses a token
belonging to one regardless. Its only credential is the session the demo
endpoint set, which is what request.user is built from here.

The allowlist is matched on URL NAMES, so it holds for /api/v1/ and its
/api/ alias alike and cannot be dodged by spelling a path differently: the
decision is about which view would run.
"""

import math
import re

from django.http import JsonResponse

from . import demo
from .throttling import GuestGlobalRateThrottle, GuestRateThrottle

READS = frozenset({"GET", "HEAD", "OPTIONS"})

# The doors that never act on the signed-in account: each works on the
# credentials or the form it is sent, or on nothing. A guest pressing Sign
# in for their real account in another tab, starting a fresh demo or asking
# for an account changes nothing about the guest account - and refusing
# them would leave a visitor stuck behind a session they cannot see.
PUBLIC_DOORS = frozenset(
    {"auth-login", "auth-demo", "auth-signup", "api-token-auth", "auth-csrf"}
)


def _chat_budget():
    return [
        GuestRateThrottle("guest_chat"),
        GuestRateThrottle("guest_chat_burst"),
        GuestGlobalRateThrottle("guest_chat_global"),
    ]


def _search_budget():
    return [
        GuestRateThrottle("guest_search"),
        GuestGlobalRateThrottle("guest_search_global"),
    ]


def _no_budget():
    return []


# What a guest may do that is not a read, by URL name, and the budget it is
# spent from on top of the view's own throttles. Asking is the point of the
# demo; search is a POST only so the query stays out of URLs and logs.
GUEST_ACTIONS = {
    "chat": _chat_budget,
    "chat-stream": _chat_budget,
    "search": _search_budget,
    "message-feedback": _no_budget,
    "auth-logout": _no_budget,
}

# Marking notifications read. Matched by the notifications API's documented
# paths (POST /notifications/{id}/read/ and /notifications/read-all/) - that
# app names its own routes.
NOTIFICATION_READ_STATE = re.compile(
    r"^/api/(?:v1/)?notifications/(?:\d+/read|read-all)/$"
)

READ_ONLY_DETAIL = (
    "This is a read-only demo workspace: you can read, search and ask "
    "questions, but not change anything. Request an account to work with "
    "your own documents."
)


def _throttled(scope: str, wait: float) -> JsonResponse:
    seconds = max(1, math.ceil(wait))
    minutes = max(1, math.ceil(seconds / 60))
    when = f"in about {minutes} minute{'s' if minutes != 1 else ''}"
    if scope.endswith("_global"):
        detail = (
            f"The demo is busy with other visitors. Try again {when}, or "
            f"request an account to use it without the demo's limits."
        )
    else:
        detail = (
            f"You have reached the demo's limit for now. Try again {when}, or "
            f"request an account to use it without the demo's limits."
        )
    response = JsonResponse({"detail": detail, "code": "demo_throttled"}, status=429)
    response["Retry-After"] = str(seconds)
    return response


class GuestReadOnlyMiddleware:
    """Must come after AuthenticationMiddleware, which builds request.user."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Reads first and for free: a member's GET never so much as loads
        # their profile here.
        if request.method in READS:
            return None
        user = getattr(request, "user", None)
        if not demo.is_guest(user):
            return None

        name = request.resolver_match.url_name if request.resolver_match else None
        if name in PUBLIC_DOORS:
            return None
        budget = GUEST_ACTIONS.get(name)
        if budget is None and NOTIFICATION_READ_STATE.match(request.path_info):
            budget = _no_budget
        if budget is None:
            from documents.observability import telemetry

            # Counted and logged, not audited: the interface offers a guest
            # nothing that ends here, so this is someone sending requests by
            # hand - and a row per refusal would let them write to the audit
            # log as fast as they can send.
            telemetry.authorization_denied("GUEST_WRITE", user_id=user.pk)
            return JsonResponse(
                {"detail": READ_ONLY_DETAIL, "code": "demo_read_only"}, status=403
            )

        # As DRF's own check_throttles: every throttle is asked, and the
        # longest wait is the one reported.
        waits = [
            (throttle.scope, throttle.wait())
            for throttle in budget()
            if not throttle.allow_request(request, None)
        ]
        if waits:
            scope, wait = max(waits, key=lambda item: item[1] or 0)
            return _throttled(scope, wait or 0)
        return None
