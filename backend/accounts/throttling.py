"""Budgets for the one-click demo (see accounts.demo).

A guest account costs nothing to get, so a guest's own budget cannot be the
only bound: whoever wants more simply starts another demo. Every limit here
therefore comes in two forms - one per address or per guest, which keeps one
visitor from crowding out the rest, and one for ALL of them together, which
is the ceiling no number of addresses or accounts can raise. Members are
never counted against the demo's budgets, so however busy the demo gets,
the people the installation is for are not slowed by it.

Rates live in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] like every other
scope (THROTTLE_DEMO*, THROTTLE_GUEST_* in .env).
"""

from rest_framework.throttling import SimpleRateThrottle


class DemoRateThrottle(SimpleRateThrottle):
    """Demo starts per address. Nobody is signed in yet, so the address is
    the only identifier there is."""

    scope = "demo"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class DemoGlobalRateThrottle(SimpleRateThrottle):
    """Demo starts from every address together."""

    scope = "demo_global"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": "all"}


class GuestRateThrottle(SimpleRateThrottle):
    """One guest's budget for one kind of request, keyed by the account.

    Applied by GuestReadOnlyMiddleware rather than by a view, on top of the
    view's own throttles: the views are shared with members and know nothing
    about guests. Works on a plain Django request as well as a DRF one -
    SimpleRateThrottle reads only the user, the address and the clock.
    """

    def __init__(self, scope: str):
        # Set before the parent reads it to look up the rate.
        self.scope = scope
        super().__init__()

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"user:{request.user.pk}",
        }


class GuestGlobalRateThrottle(GuestRateThrottle):
    """The same kind of request from every guest together."""

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": "all"}
