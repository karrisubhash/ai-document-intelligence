"""Idempotency keys for the POSTs a client may realistically send twice (spec 16).

An upload whose 202 is lost to a dropped connection leaves the client unable
to tell whether it landed, and resending it created a second document - a
second copy in search, a second processing job, a second bill. A client that
sends an `Idempotency-Key` header gets the first request's result back on a
retry instead.

The rules, all of them deliberate:

- A key is scoped to (endpoint, user). Two users cannot collide, and one
  user's key can never replay another user's resource.
- The first request CLAIMS the key with `cache.add`, which is atomic in
  Redis, so two concurrent sends cannot both create. The loser gets 409 and
  may retry once the first has finished.
- A key reused for a DIFFERENT request is a client bug. It gets 422, never
  the other request's result. An uploaded file is compared by its content.
- A request that fails releases its key, so the retry runs for real. One
  that dies before it can (a killed worker) holds the key only for
  PENDING_TTL_SECONDS.
- A replay re-reads the resource through the caller's authorization, so it
  reports the current state and never outlives the caller's access.
- No header, no behaviour change.

Keys live 24 hours in the shared cache. Losing the cache loses only the
deduplication window, never data.
"""

import hashlib

from django.core.cache import cache
from django.core.files import File
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
TTL_SECONDS = 24 * 60 * 60
# How long a claim lasts with no outcome recorded. finish() and abandon()
# replace or drop it at once; this bounds only a request killed before
# either ran (a worker timeout, the OOM killer, a deploy's grace period).
# Claimed for the full day, such a key answered every retry with 409 until
# the next day. Longer than any request may run: gunicorn kills a worker
# after GUNICORN_TIMEOUT, 120 s by default.
PENDING_TTL_SECONDS = 10 * 60
MAX_KEY_LENGTH = 255
_PENDING = "pending"


class IdempotencyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "A request with this Idempotency-Key is still in progress."
    default_code = "idempotency_in_progress"


class IdempotencyKeyReused(APIException):
    status_code = 422
    default_detail = "This Idempotency-Key was already used for a different request."
    default_code = "idempotency_key_reused"


def fingerprint(*parts):
    """A stable digest of the request fields that make two sends 'the same'.

    An uploaded file counts by its content, read in chunks and rewound for
    whoever reads it next. By name and size alone, an edited file of the same
    length was "the same request": the retry got the first upload's result
    and its own content was silently dropped. Each part is hashed on its own,
    so no choice of values can make two different lists of parts collide.
    """
    combined = hashlib.sha256()
    for part in parts:
        digest = hashlib.sha256()
        if isinstance(part, File):
            for chunk in part.chunks():
                digest.update(chunk)
            part.seek(0)
        else:
            digest.update(str(part).encode("utf-8"))
        combined.update(digest.digest())
    return combined.hexdigest()


class Idempotency:
    """One request's claim on its key. Every method is a no-op without one."""

    def __init__(self, request, scope, *fingerprint_parts):
        raw = request.headers.get(HEADER)
        self.key = None
        if raw is None:
            return
        raw = raw.strip()
        if (
            not raw
            or len(raw) > MAX_KEY_LENGTH
            or not raw.isascii()
            or not raw.isprintable()
        ):
            raise ValidationError(
                {HEADER: [f"Must be 1-{MAX_KEY_LENGTH} printable ASCII characters."]}
            )
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        self.key = f"idem:{scope}:{request.user.pk}:{digest}"
        # Only once a key is known to be present: fingerprinting reads an
        # upload end to end, which a request without one should not pay for.
        self.fingerprint = fingerprint(*fingerprint_parts)

    def start(self):
        """Claim the key. Returns the stored primary key to replay, or None."""
        if self.key is None:
            return None
        if cache.add(
            self.key, {"state": _PENDING, "fp": self.fingerprint}, PENDING_TTL_SECONDS
        ):
            return None
        stored = cache.get(self.key)
        if stored is None:
            # Expired between add() and get(): claim it again rather than guess.
            return self.start()
        if stored["fp"] != self.fingerprint:
            raise IdempotencyKeyReused()
        if stored["state"] == _PENDING:
            raise IdempotencyConflict()
        return stored["pk"]

    def finish(self, pk):
        if self.key is not None:
            cache.set(
                self.key,
                {"state": "done", "fp": self.fingerprint, "pk": pk},
                TTL_SECONDS,
            )

    def abandon(self):
        if self.key is not None:
            cache.delete(self.key)
