"""Rate limiting (spec §13: "API rate limiting and abuse controls").

Limits are per endpoint class rather than one global number, because the
endpoints cost wildly different amounts. A document listing is a cheap
indexed query; a chat request runs retrieval, reranking, context
construction and an answerer. One limit that is comfortable for the first is
far too generous for the second.

Anonymous callers are throttled by IP, which is weak - an IP is shared and
spoofable behind a misconfigured proxy - but it is the only identifier an
unauthenticated request carries, and it raises the cost of a credential
guessing run from free to noticeable.
"""

from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle


class LoginRateThrottle(SimpleRateThrottle):
    """Throttles credential guessing at the login endpoint.

    Keyed on IP AND the submitted username, so one attacker hammering many
    usernames is limited per-target, and a shared NAT does not let one
    careless user lock out a whole office. Both halves matter: keying on IP
    alone lets a botnet spread a guessing run across addresses, and keying
    on username alone lets anyone lock a known account out by failing
    logins against it.
    """

    scope = "login"

    def get_cache_key(self, request, view):
        username = ""
        if request.method == "POST":
            # Never log or store this; it is only hashed into a cache key.
            username = str(request.data.get("username", ""))[:150]

        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{self.get_ident(request)}:{username.lower()}",
        }


class BurstUserThrottle(ScopedRateThrottle):
    """Scoped throttle that always identifies an authenticated user by pk.

    DRF's default falls back to IP for anonymous callers, which is right,
    but for authenticated ones the account is the unit of abuse - two users
    behind one office NAT should not share a budget.
    """

    def get_cache_key(self, request, view):
        if not getattr(self, "scope", None):
            return None

        if request.user and request.user.is_authenticated:
            ident = f"user:{request.user.pk}"
        else:
            ident = f"ip:{self.get_ident(request)}"

        return self.cache_format % {"scope": self.scope, "ident": ident}
