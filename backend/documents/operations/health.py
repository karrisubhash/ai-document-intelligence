"""Dependency checks, shared by /ready/ and the system status screen.

Each check answers one question - can this process use that dependency
right now - and returns a status and a latency, never an exception and never
a connection string. A readiness probe that 500s, or that echoes the broker
URL with its password into a load balancer's logs, is worse than none.
"""

import time
import uuid

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection

from documents.observability import queues

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


def _timed(check):
    started = time.monotonic()
    try:
        detail = check()
        outcome = {"status": OK}
        if detail:
            outcome["detail"] = detail
    except Exception as exc:
        # The exception CLASS, not its message: messages from database and
        # network drivers routinely include host names and user names.
        outcome = {"status": FAILED, "error": type(exc).__name__}
    outcome["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
    return outcome


def check_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def check_cache():
    """Redis, which carries throttle counters and sessions."""
    key = f"adi:health:{uuid.uuid4().hex}"
    cache.set(key, "1", timeout=10)
    if cache.get(key) != "1":
        raise RuntimeError("cache did not return the value just written")
    cache.delete(key)


def check_broker():
    url = queues.broker_url()
    if not url.startswith("redis"):
        return "broker is not Redis; not probed"
    import redis

    client = redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    client.ping()


def check_storage():
    """Write, read back and delete a probe object.

    Existence checks are not enough: a read-only mount or a bucket policy
    that forbids PutObject passes `exists()` and fails every upload.
    """
    name = default_storage.save(f"health/{uuid.uuid4().hex}.probe", ContentFile(b"ok"))
    try:
        with default_storage.open(name, "rb") as handle:
            if handle.read() != b"ok":
                raise RuntimeError("storage returned different bytes")
    finally:
        default_storage.delete(name)


CHECKS = {
    "database": check_database,
    "cache": check_cache,
    "broker": check_broker,
    "storage": check_storage,
}

# What /ready/ requires. Storage is deliberately not in it: the probe runs
# every few seconds, and writing an object to S3 that often is a cost and a
# source of noise. The system status screen runs it on demand instead.
READINESS = ("database", "cache", "broker")


def check_all(names=None) -> dict:
    return {name: _timed(CHECKS[name]) for name in (names or CHECKS)}


def queue_depths() -> dict:
    try:
        return queues.collect()
    except Exception:  # pragma: no cover - collect() already never raises
        return {}


def ready() -> tuple:
    """(is_ready, per-component results) for the readiness endpoint."""
    results = check_all(READINESS)
    return all(result["status"] == OK for result in results.values()), results
