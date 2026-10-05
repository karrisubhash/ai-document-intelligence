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


def submitted_account(request) -> str:
    """The account a credential check is aimed at, as a throttle key.

    The signed-in user for an authenticated request (a password change).
    Otherwise the submitted username, normalised the way the serializer
    does before authenticating - stripped of surrounding whitespace - and
    then lower-cased. Keyed on the RAW value, "alice", "alice " and
    "alice\\t" were separate buckets that all authenticated as alice, so
    padding the name with whitespace reset the limit on every guess.

    Never logged or stored; it only becomes part of a cache key.
    """
    if request.user and request.user.is_authenticated:
        return f"user:{request.user.pk}"
    if request.method != "POST":
        return ""
    # A JSON array or string body has no .get(); that is a 400 for the
    # view to give, not a 500 here.
    data = request.data if isinstance(request.data, dict) else {}
    return str(data.get("username", "")).strip()[:150].lower()


class LoginRateThrottle(SimpleRateThrottle):
    """Throttles credential guessing at the login endpoint.

    Keyed on IP AND the target account, so one attacker hammering many
    usernames is limited per-target, and a shared NAT does not let one
    careless user lock out a whole office. On its own it does not bound a
    guessing run spread across many addresses; AccountLoginRateThrottle
    does that.
    """

    scope = "login"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{self.get_ident(request)}:{submitted_account(request)}",
        }


class AccountLoginRateThrottle(SimpleRateThrottle):
    """A ceiling on attempts against ONE account, from every address at once.

    NIST SP 800-63B (5.2.2) caps failed attempts on a single account at 100.
    The per-address throttle cannot enforce that: a botnet gives each
    address its own budget. The ceiling is set far above anything a person
    produces, because keying on the account alone is exactly what lets a
    stranger lock that account out - so the limit has to bound a guessing
    run without being cheap to trip on purpose.
    """

    scope = "login_account"

    def get_cache_key(self, request, view):
        account = submitted_account(request)
        if not account:
            return None
        return self.cache_format % {"scope": self.scope, "ident": account}


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
