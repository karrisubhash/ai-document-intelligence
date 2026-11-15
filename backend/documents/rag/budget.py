"""The shared LLM budget: cooldowns, daily call counters and exhaustion.

Free tiers meter requests per model per DAY (Gemini: 20) and per MINUTE (5),
and the second limit is hit long before the first. Until this module the
evidence of both lived in a dict inside each process: every gunicorn worker
and every Celery child rediscovered the same 429 for itself, paid a doomed
round trip to do it, and nobody could see which models were benched or how
much of the day's allowance was left.

So the state lives in the Django cache instead - Redis in every real
deployment, which the throttles already depend on for the same reason: a
limit that is not shared is not a limit. Per link ("provider:model"):

  cool:{link}           benched until a time, after a 429 or a 503
  exhausted:{link}      the provider said today's quota is spent (a per-day
                        429); lifts at that provider's next quota midnight
  calls:{link}:{day}    requests sent today, by the provider's own day
  error:{link}          the last failure: exception type, status, when
  success:{link}        when it last answered

A request is counted when it is SENT, before the answer arrives, so two
processes cannot both spend the last call of the day; a request the provider
refused with a 429 is handed back, because the provider does not count it
either. Everything else - a timeout, a 500, a reply that failed grounding -
was served and billed, so it stays counted.

THIS IS BOOKKEEPING, NOT A DEPENDENCY. If the cache is unreachable, every
operation falls back to a per-process store and says so in the log once a
minute: answers keep flowing exactly as they did when this state was
per-process, rather than chat failing because a counter could not be read.
"""

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger("adi.llm")

DEFAULT_COOLDOWN_SECONDS = 60.0
# A per-day quota will not lift in a minute. The link is also marked
# exhausted until its provider's next quota midnight (mark_exhausted); this
# shorter bench is what a link reports while the mark is in place, and the
# floor under it if the mark is lost.
DAILY_COOLDOWN_SECONDS = 3600.0
OVERLOAD_COOLDOWN_SECONDS = 30.0
_RETRY_IN = re.compile(r"retry in ([0-9]+(?:\.[0-9]+)?)\s*s", re.I)

# How each provider words a spent DAILY allowance. Gemini puts a quota id in
# the body ("GenerateRequestsPerDayPerProjectPerModel-FreeTier"), Groq names
# the window ("requests per day (RPD)", "tokens per day (TPD)"), OpenRouter
# its limit ("free-models-per-day") and GitHub Models its bucket
# ("UserByModelByDay"). A per-minute 429 matches none of them.
_DAILY_QUOTA = re.compile(r"per[\s_-]?day|by[\s_-]?day|\bRPD\b|\bTPD\b", re.I)

# Counters outlive their day by a margin, so a request in flight across
# midnight can still hand its reservation back.
COUNTER_TTL_SECONDS = 2 * 86400
HISTORY_TTL_SECONDS = 7 * 86400

PREFIX = "llm:v1"

# How often a cache outage is logged. Every call fails while it lasts, and a
# warning per question would bury everything else in the log.
_OUTAGE_LOG_INTERVAL = 60.0


def _now() -> float:
    """Wall-clock seconds. Not monotonic: other processes read these."""
    return time.time()


def _status(exc) -> int | None:
    return getattr(exc, "status_code", None)


def _retry_after(exc) -> str:
    """The provider's Retry-After header, when the failure carried one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter("retry-after", "") or "")


def is_daily_quota(exc) -> bool:
    """Whether a 429 says the DAY's allowance is spent, not the minute's."""
    try:
        return _status(exc) == 429 and bool(_DAILY_QUOTA.search(str(exc)))
    except Exception:  # str() of an exception should never raise; be sure
        return False


def is_timeout(exc) -> bool:
    """Whether a failure is a request that was waited for and never answered (the
    SDK's APITimeoutError, httpx's ReadTimeout, the builtin TimeoutError)."""
    return isinstance(exc, TimeoutError) or any(
        "timeout" in cls.__name__.lower() for cls in type(exc).__mro__
    )


def _cooldown_seconds(exc) -> float:
    """How long a 429 should bench its model. Never raises.

    Gemini sends no Retry-After header; it puts "Please retry in 14.5s" and
    a quota id in the body. A per-DAY quota will not lift in a minute.
    """
    try:
        if _status(exc) == 503 or is_timeout(exc):
            # Overloaded, or hung: not rate-limited, and usually over in
            # seconds, so a short bench.
            return OVERLOAD_COOLDOWN_SECONDS
        text = str(exc)
        if "PerDay" in text or (_status(exc) == 429 and _DAILY_QUOTA.search(text)):
            return DAILY_COOLDOWN_SECONDS
        match = _RETRY_IN.search(text)
        if match:
            return max(1.0, float(match.group(1)))
        header = _retry_after(exc)
        if header:
            # Seconds, or an HTTP date (RFC 9110) - which float() refuses.
            return max(1.0, float(header))
    except (TypeError, ValueError):
        pass
    return DEFAULT_COOLDOWN_SECONDS


# --- Days and limits -----------------------------------------------------------


def _zone(name: str):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("llm.unknown_quota_zone", extra={"zone": name})
        return UTC


def quota_day(zone: str, now: float) -> str:
    """The provider's calendar day at `now`, as YYYY-MM-DD."""
    return datetime.fromtimestamp(now, _zone(zone)).date().isoformat()


def next_reset(zone: str, now: float) -> datetime:
    """The next midnight in `zone` after `now`, as an aware UTC datetime."""
    tz = _zone(zone)
    local = datetime.fromtimestamp(now, tz)
    tomorrow = (local + timedelta(days=1)).date()
    midnight = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=tz)
    return midnight.astimezone(UTC)


def parse_limits(raw) -> dict:
    """LLM_DAILY_LIMITS as a dict: "groq=100,gemini:gemini-3.5-flash=40,*=0".

    A dict is taken as it is. Keys are `provider:model`, `provider`, or `*`
    for every provider; the most specific key wins. Malformed entries are
    logged and ignored rather than failing the process at import.
    """
    if isinstance(raw, dict):
        return {str(key).strip().lower(): int(value) for key, value in raw.items()}
    limits = {}
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.rpartition("=")
        try:
            limits[key.strip().lower()] = max(0, int(value.strip()))
        except ValueError:
            logger.warning("llm.bad_daily_limit", extra={"entry": item})
    return limits


def daily_limit_for(provider: str, model: str, default: int) -> int:
    """The requests a day one link may make; 0 means no limit is enforced."""
    limits = parse_limits(getattr(settings, "LLM_DAILY_LIMITS", ""))
    for key in (f"{provider}:{model}".lower(), provider.lower(), "*"):
        if key in limits:
            return limits[key]
    return default


def chat_reserve() -> int:
    return max(0, int(getattr(settings, "LLM_CHAT_RESERVE", 20) or 0))


# --- Stores ---------------------------------------------------------------------


class _LocalStore:
    """The per-process fallback, with the cache API's semantics."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def _live(self, key, now):
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and expires <= now:
            del self._data[key]
            return None
        return entry

    def get_many(self, keys):
        now = _now()
        with self._lock:
            found = {}
            for key in keys:
                entry = self._live(key, now)
                if entry is not None:
                    found[key] = entry[0]
            return found

    def set(self, key, value, timeout):
        with self._lock:
            self._data[key] = (value, _now() + timeout if timeout else None)

    def incr(self, key, delta, timeout):
        now = _now()
        with self._lock:
            entry = self._live(key, now)
            if entry is None:
                if delta < 0:
                    return 0
                self._data[key] = (delta, now + timeout)
                return delta
            value = max(0, entry[0] + delta)
            self._data[key] = (value, entry[1])
            return value

    def delete_many(self, keys):
        with self._lock:
            for key in keys:
                self._data.pop(key, None)


class _CacheStore:
    """The Django cache, which is Redis wherever more than one process runs."""

    def __init__(self, alias):
        self.alias = alias

    @property
    def cache(self):
        return caches[self.alias]

    def get_many(self, keys):
        return self.cache.get_many(keys)

    def set(self, key, value, timeout):
        self.cache.set(key, value, timeout)

    def incr(self, key, delta, timeout):
        cache = self.cache
        try:
            return cache.incr(key, delta)
        except ValueError:
            # Missing: the first call of the day on this link. add() is
            # atomic, so of two processes racing here one creates the key
            # and the other increments it.
            if delta < 0:
                return 0
            if cache.add(key, delta, timeout):
                return delta
            return cache.incr(key, delta)

    def delete_many(self, keys):
        self.cache.delete_many(keys)


# --- The broker -----------------------------------------------------------------


@dataclass
class LinkState:
    calls_today: int = 0
    # Epoch seconds; 0 when not benched.
    cooling_until: float = 0.0
    exhausted_until: float = 0.0
    last_error: dict | None = None
    last_success_at: float | None = None


@dataclass
class Reservation:
    """One counted request, so a refund decrements the counter it counted
    on - not the next day's, if the reply arrives after midnight."""

    key: str = ""
    count: int = 0


@dataclass
class _Outage:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_logged: float = 0.0


def _safe_id(link_id: str) -> str:
    # Cache keys must not hold whitespace or control characters (memcached
    # refuses them, and Django warns for every backend). Model ids never
    # should, but they come from settings.
    return re.sub(r"[^A-Za-z0-9._:/@+-]", "_", link_id)[:200]


class Broker:
    """Reads and writes the shared state for a set of links.

    A link is anything with `id`, `daily_limit` and `quota_tz` attributes
    (documents.rag.gateway.Link).
    """

    def __init__(self, alias: str = "default"):
        self._store = _CacheStore(alias)
        self._local = _LocalStore()
        self._outage = _Outage()
        # Every key this process has touched, so reset() can find them.
        self._touched = set()

    # -- plumbing --------------------------------------------------------

    def _call(self, operation, *args):
        try:
            return getattr(self._store, operation)(*args)
        except Exception as exc:
            now = _now()
            with self._outage.lock:
                if now - self._outage.last_logged >= _OUTAGE_LOG_INTERVAL:
                    self._outage.last_logged = now
                    logger.warning(
                        "llm.budget_cache_unavailable",
                        extra={
                            "error": type(exc).__name__,
                            "fallback": "per-process state until the cache answers",
                        },
                    )
            return getattr(self._local, operation)(*args)

    def _key(self, kind: str, link, *parts) -> str:
        key = ":".join((PREFIX, kind, _safe_id(link.id), *parts))
        self._touched.add(key)
        return key

    def _calls_key(self, link, now: float) -> str:
        return self._key("calls", link, quota_day(link.quota_tz, now))

    # -- reading ----------------------------------------------------------

    def states(self, links, now: float | None = None) -> dict:
        """Every link's state in one round trip, keyed by link id."""
        now = _now() if now is None else now
        keys = {}
        for link in links:
            keys[link.id] = {
                "cool": self._key("cool", link),
                "exhausted": self._key("exhausted", link),
                "calls": self._calls_key(link, now),
                "error": self._key("error", link),
                "success": self._key("success", link),
            }
        wanted = [key for per_link in keys.values() for key in per_link.values()]
        found = self._call("get_many", wanted) if wanted else {}

        states = {}
        for link_id, per_link in keys.items():
            cool = found.get(per_link["cool"]) or {}
            exhausted = found.get(per_link["exhausted"]) or {}
            states[link_id] = LinkState(
                calls_today=int(found.get(per_link["calls"]) or 0),
                cooling_until=float(cool.get("until") or 0.0),
                exhausted_until=float(exhausted.get("until") or 0.0),
                last_error=found.get(per_link["error"]),
                last_success_at=found.get(per_link["success"]),
            )
        return states

    # -- counting ---------------------------------------------------------

    def reserve(self, link, now: float | None = None) -> Reservation:
        """Count one request against today's allowance, before it is sent."""
        now = _now() if now is None else now
        key = self._calls_key(link, now)
        return Reservation(
            key=key, count=self._call("incr", key, 1, COUNTER_TTL_SECONDS)
        )

    def refund(self, reservation: Reservation) -> None:
        """Hand back a request the provider refused without serving."""
        if reservation.key:
            self._call("incr", reservation.key, -1, COUNTER_TTL_SECONDS)

    # -- outcomes ---------------------------------------------------------

    def cool(self, link, exc, now: float | None = None) -> float:
        """Bench a link after a 429 or 503; returns when it may be tried."""
        now = _now() if now is None else now
        seconds = _cooldown_seconds(exc)
        until = now + seconds
        self._call(
            "set",
            self._key("cool", link),
            {"until": until, "status": _status(exc)},
            math.ceil(seconds) + 1,
        )
        return until

    def mark_exhausted(self, link, now: float | None = None) -> float:
        """Today's quota is spent; lifts at the provider's next midnight."""
        now = _now() if now is None else now
        until = next_reset(link.quota_tz, now).timestamp()
        self._call(
            "set",
            self._key("exhausted", link),
            {"until": until, "day": quota_day(link.quota_tz, now)},
            max(1, math.ceil(until - now)),
        )
        return until

    def clear(self, link) -> None:
        """Forget a bench and an exhaustion mark: the link just answered."""
        self._call(
            "delete_many", [self._key("cool", link), self._key("exhausted", link)]
        )

    def record_error(self, link, exc, now: float | None = None) -> None:
        now = _now() if now is None else now
        self._call(
            "set",
            self._key("error", link),
            # The exception CLASS and the status, never the message: provider
            # error text can echo request content, and a router's upstream
            # writes it, not us.
            {"type": type(exc).__name__, "status": _status(exc), "at": now},
            HISTORY_TTL_SECONDS,
        )

    def record_success(self, link, now: float | None = None) -> None:
        now = _now() if now is None else now
        self._call("set", self._key("success", link), now, HISTORY_TTL_SECONDS)

    def reset(self) -> None:
        """For tests: forget everything this process has touched."""
        touched = list(self._touched)
        self._touched.clear()
        if touched:
            self._call("delete_many", touched)
            self._local.delete_many(touched)


# One per process. Every gateway shares it; what it stores is shared with
# every other process through the cache.
broker = Broker()


def reset() -> None:
    """For tests: forget every remembered 429, exhaustion mark and count."""
    broker.reset()
