"""Structured logging, and the guarantee that it never leaks a document.

Section 13 says plainly: "Do not expose confidential document content
through ordinary application logs." A convention cannot deliver that - one
`logger.info(f"answering: {question}")` written in a hurry undoes it, and
the failure is invisible until someone reads the logs.

So redaction is enforced at the formatter, on every record, whatever the
caller passed:

  * fields named for content (question, answer, text, password, token, ...)
    are replaced wholesale, never truncated - a truncated question is still
    a leaked question;
  * any remaining string long enough to be prose is truncated to a length
    that can carry an identifier but not a paragraph;
  * the formatted message itself is length-capped for the same reason.

The cost is that a genuinely long non-sensitive string gets clipped in the
logs. That is the right trade: the failure mode of over-redaction is a
slightly less convenient log line, and the failure mode of under-redaction
is confidential documents sitting in a log aggregator.
"""

import json
import logging
import re

from .context import get_request_id

REDACTED = "[redacted]"

# Names whose VALUE is content or a secret, regardless of what is in it.
SENSITIVE_KEYS = frozenset(
    {
        "question",
        "answer",
        "query",
        "text",
        "content",
        "body",
        "passage",
        "passages",
        "prompt",
        "completion",
        "extracted_text",
        "citations",
        "comment",
        "expected_answer",
        "password",
        "token",
        "secret",
        "authorization",
        "api_key",
        "email",
        "filename",
        "file_name",
        "title",
    }
)

# Long enough for an id, a status, an error code or a stage name; too short
# for a sentence of a document.
MAX_VALUE_LENGTH = 120
MAX_MESSAGE_LENGTH = 500

# Standard LogRecord attributes, so everything else in __dict__ is caller
# supplied and belongs in the structured payload.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname
    levelno lineno module msecs message msg name pathname process
    processName relativeCreated stack_info thread threadName taskName""".split()
)

_LONG_RUN = re.compile(r"\S{200,}")


def redact_value(key: str, value):
    """One field, redacted according to its name and its shape.

    A long string is replaced ENTIRELY, not truncated. Truncation felt like
    a reasonable middle ground until a test showed what it actually emits:
    the first 120 characters of a confidential document, which is a leak
    with extra steps. The same reasoning that makes a truncated question
    unacceptable applies to a truncated anything - so the length is
    reported and the content is not.
    """
    # A number cannot carry document content, whatever it is called. Without
    # this, `citations=len(answer.citations)` logs as "[redacted]" - the
    # count is an operational signal and redacting it buys nothing.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value

    if key.lower() in SENSITIVE_KEYS:
        return REDACTED

    if isinstance(value, str):
        if len(value) > MAX_VALUE_LENGTH:
            return f"[redacted: {len(value)} chars]"
        return value

    if isinstance(value, dict):
        return {k: redact_value(k, v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        # Indexes are not names, so list items are judged on shape alone.
        return [redact_value("", item) for item in value][:20]

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    return redact_value(key, str(value))


class StructuredFormatter(logging.Formatter):
    """JSON lines, with the request id attached and every field redacted."""

    def format(self, record: logging.LogRecord) -> str:
        message = self._safe_message(record)

        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = redact_value(key, value)

        if record.exc_info:
            # The traceback text, capped. Stack frames can quote source
            # lines, and a source line can contain a document literal.
            payload["exception"] = self.formatException(record.exc_info)[
                -MAX_MESSAGE_LENGTH:
            ]

        return json.dumps(payload, default=str, ensure_ascii=False)

    @staticmethod
    def _safe_message(record: logging.LogRecord) -> str:
        """The message, with document content kept out of it.

        Our own loggers go through `log_event`, whose message is a short
        event identifier with no spaces - `rag.answered`, `pipeline.stage`.
        So for `adi.*` records anything past the first token is not an event
        name, it is interpolated data, and it is dropped. That closes the
        careless `logger.info(f"answering {question}")` case completely
        rather than trimming it to a shorter leak.

        Records from Django and third-party libraries are prose by design
        ("Not Found: /x"), so those are length-capped instead. They do not
        handle document content.
        """
        message = record.getMessage()

        if record.name.startswith("adi"):
            event = message.split(None, 1)
            head = event[0] if event else ""
            if len(event) > 1 or len(head) > MAX_VALUE_LENGTH:
                return f"{head[:MAX_VALUE_LENGTH]} {REDACTED}".strip()
            return head

        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH] + "...[truncated]"

        # A single enormous token is almost always an embedded blob.
        return _LONG_RUN.sub(REDACTED, message)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger, level, event: str, **fields) -> None:
    """Emit one structured event.

    `event` is a short stable identifier - `document.processed`,
    `rag.abstained` - so log lines can be grouped without parsing prose.
    Everything else goes through redaction on the way out.
    """
    logger.log(level, event, extra=fields)
