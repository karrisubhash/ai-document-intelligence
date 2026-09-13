"""The request id that ties a log line to the request that produced it.

A contextvar rather than thread-local storage: Django can serve on async
handlers, and a thread-local silently returns the wrong id there rather than
failing, which is the worst way for a correlation id to break.

The id survives into Celery explicitly - see `documents.tasks` - because a
worker runs in a different process and would otherwise start every document
with no thread back to the upload that queued it. Section 20 asks for a
request to be followable from API through retrieval; that thread is the
whole point.
"""

import uuid
from contextvars import ContextVar

_request_id: ContextVar = ContextVar("adi_request_id", default="")

# Accepted from a caller so a trace can span a reverse proxy or a frontend,
# but never trusted verbatim - see set_request_id.
HEADER = "HTTP_X_REQUEST_ID"
RESPONSE_HEADER = "X-Request-ID"

MAX_LENGTH = 64


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str = "") -> str:
    """Adopt a caller's id, or mint one.

    A client-supplied id is length-capped and stripped of anything that is
    not alphanumeric, dash or underscore. It ends up in log files that other
    tools parse, so a caller must not be able to inject newlines, quotes or
    JSON structure into them through this header.
    """
    cleaned = "".join(
        character
        for character in (value or "")[:MAX_LENGTH]
        if character.isalnum() or character in "-_"
    )

    _request_id.set(cleaned or new_request_id())
    return _request_id.get()


def get_request_id() -> str:
    return _request_id.get()


def reset_request_id() -> None:
    _request_id.set("")
