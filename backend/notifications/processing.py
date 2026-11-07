"""News about a document's processing: it is ready, or why it could not be.

Driven by saves of ProcessingJob (see receivers.py) rather than by the
pipeline calling in: the pipeline is not this app's to edit, and every
terminal transition it makes is a save of the job with "status" in
update_fields - pipeline._mark_completed and _mark_failed, and
services._mark_stalled for a run declared dead. The one terminal write that
is not a save, services.enqueue marking a job FAILED with an UPDATE when the
broker refuses it, sends nothing: no signal fires for it.

What is announced, and to whom:

* Only an outcome that speaks for the document: the job is for its CURRENT
  version and no later run for that version exists. The same rule as
  pipeline._set_document_status - a run on a superseded version, or a
  stalled run that Reprocess has already replaced, changes nothing the
  owner can see, so it is not news.
* To the document's owner and the uploader of that version, each only if
  their account is active and they can still read the document
  (authorization.can_view_document) - a notification names the document,
  and a title is content.
* Once per job and outcome. A Celery redelivery that runs a failed job
  again, or any second save of a finished job, finds its event already
  recorded in the recipient's notifications and sends nothing. A retry the
  task makes itself never gets here at all: a transient failure is recorded
  as QUEUED (pipeline._mark_retrying), not FAILED.
* Coalesced. A batch upload finishing over a few minutes is one
  notification that keeps updating - "12 documents are ready" - for as long
  as each result arrives within NOTIFICATIONS_COALESCE_MINUTES of the last
  and the person has not read it. One entry per DOCUMENT, so a document
  that failed and then succeeded on a reprocess reads as ready, once.

The text is composed here, in plain language: an error code becomes a
reason a person can act on, and an internal error message (which can carry
paths and host names) is never shown.
"""

import logging
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.db import transaction

from documents.authorization import can_view_document
from documents.models import ProcessingJob
from documents.processing.errors import ErrorCode

from . import service
from .models import Notification
from .service import Content

logger = logging.getLogger("adi.notifications")

KEY = "documents.processed"
KIND_READY = "document.ready"
KIND_FAILED = "document.failed"
KIND_QUARANTINED = "document.quarantined"
KINDS = (KIND_READY, KIND_FAILED, KIND_QUARANTINED)

READY, FAILED, QUARANTINED = "ready", "failed", "quarantined"
TERMINAL = (ProcessingJob.Status.COMPLETED, ProcessingJob.Status.FAILED)

# A notification names at most this many documents; the next one starts a
# new notification. Bounds the payload, and a person cannot act on a list
# of hundreds in a bell anyway - the library is where those are.
MAX_DOCUMENTS = 50
# How much of a document title is quoted, and how many are named in a body.
QUOTED_TITLE = 80
NAMED = 3

# After "“Fee structure” could not be processed: ". What the person can do
# about it, not what the code was.
PLAIN_REASON = {
    ErrorCode.MISSING_FILE: "the uploaded file was missing",
    ErrorCode.EMPTY_FILE: "the file is empty",
    ErrorCode.UNSUPPORTED_FILE_TYPE: "this type of file is not supported",
    ErrorCode.FILE_TOO_LARGE: "the file is over the size limit",
    ErrorCode.TOO_MANY_PAGES: "it has more pages than the limit",
    ErrorCode.CORRUPT_DOCUMENT: "the file appears to be damaged",
    ErrorCode.EXTRACTION_FAILED: "its text could not be read",
    ErrorCode.OCR_FAILED: "its scanned pages could not be read",
    ErrorCode.EMPTY_EXTRACTION: "no readable text was found in it",
    ErrorCode.CLASSIFICATION_FAILED: "it could not be classified",
    ErrorCode.FIELD_EXTRACTION_FAILED: "its fields could not be extracted",
    ErrorCode.CHUNKING_FAILED: "it could not be indexed for search",
    ErrorCode.MALWARE_DETECTED: "the security scan flagged the file",
    ErrorCode.SCAN_FAILED: "the security scan could not check it",
    ErrorCode.INFRASTRUCTURE_ERROR: "a service it needs was unavailable",
    ErrorCode.TIMEOUT: "it took longer than the time limit",
    ErrorCode.STALLED: "processing was interrupted",
}
UNKNOWN_REASON = "something went wrong while reading it"

# Failures that say nothing about the file: running it again may well work.
NOT_THE_FILE = frozenset(
    {
        ErrorCode.INFRASTRUCTURE_ERROR,
        ErrorCode.SCAN_FAILED,
        ErrorCode.STALLED,
        ErrorCode.UNEXPECTED_ERROR,
        ErrorCode.CLASSIFICATION_FAILED,
        ErrorCode.FIELD_EXTRACTION_FAILED,
        ErrorCode.CHUNKING_FAILED,
    }
)


def job_saved(sender, instance, created, update_fields=None, **kwargs):
    """post_save receiver for ProcessingJob.

    The work happens after the commit, and never inside the pipeline's own
    transaction: a notification that failed to write must not roll back a
    finished job, and one written for a job whose transaction then rolled
    back would announce something that did not happen. Outside a
    transaction (pipeline._mark_failed runs in autocommit) on_commit runs
    at once - which is why announce() decides from the job and its version
    rather than from the document's status, not yet written at that point.
    """
    if created or instance.status not in TERMINAL:
        return
    if update_fields is not None and "status" not in update_fields:
        return
    job_id = instance.pk
    transaction.on_commit(lambda: safely(announce, job_id))


def safely(function, *args):
    """Run a producer; log, never raise. Notifications are a courtesy - a
    failure to send one must not fail the upload, approval or run that
    caused it."""
    try:
        return function(*args)
    except Exception:
        logger.exception(
            "notifications.producer_failed", extra={"producer": function.__name__}
        )
        return None


def announce(job_id: int) -> int:
    """Tell the people a finished job concerns. Returns how many were told."""
    job = (
        ProcessingJob.objects.select_related(
            "document_version__document__owner__profile",
            "document_version__uploaded_by__profile",
        )
        .filter(pk=job_id)
        .first()
    )
    if job is None or job.status not in TERMINAL:
        return 0
    version = job.document_version
    document = version.document
    if document.current_version_id != version.pk or document.is_archived:
        return 0
    if ProcessingJob.objects.filter(
        document_version_id=version.pk, pk__gt=job.pk
    ).exists():
        return 0

    outcome, reason = outcome_of(job)
    event = f"{job.pk}:{job.status}"
    coalescing = window()
    key = KEY if coalescing else f"{KEY}:{document.pk}"

    told = 0
    for person in recipients(document, version):
        if already_announced(person, event):
            continue
        written = service.coalesce(
            person,
            key,
            partial(
                merge, document=document, outcome=outcome, reason=reason, event=event
            ),
            window=coalescing,
        )
        told += written is not None
    return told


def outcome_of(job) -> tuple[str, str]:
    if job.status == ProcessingJob.Status.COMPLETED:
        return READY, ""
    if job.error_code == ErrorCode.MALWARE_DETECTED:
        return QUARANTINED, job.error_code
    return FAILED, job.error_code or ErrorCode.UNEXPECTED_ERROR


def recipients(document, version) -> list:
    people = []
    for person in (document.owner, version.uploaded_by):
        if person is None or not person.is_active:
            continue
        if any(existing.pk == person.pk for existing in people):
            continue
        if not can_view_document(person, document):
            continue
        people.append(person)
    return people


def already_announced(person, event: str) -> bool:
    return Notification.objects.filter(
        recipient=person, kind__in=KINDS, payload__contains={"events": [event]}
    ).exists()


def window() -> timedelta | None:
    minutes = getattr(settings, "NOTIFICATIONS_COALESCE_MINUTES", 10)
    return timedelta(minutes=minutes) if minutes > 0 else None


def merge(previous, *, document, outcome, reason, event):
    """The open notification with this outcome folded in (coalesce's build)."""
    payload = previous.payload if previous is not None else {}
    entries = [dict(entry) for entry in payload.get("documents", [])]
    events = list(payload.get("events", []))
    if event in events:
        return None

    fresh = False
    position = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.get("id") == document.pk
        ),
        None,
    )
    if position is not None:
        # The latest word on a document replaces the earlier one.
        entries.pop(position)
    elif len(entries) >= MAX_DOCUMENTS:
        entries, events, fresh = [], [], True

    entries.append(
        {
            "id": document.pk,
            "title": _shorten(document.title, QUOTED_TITLE),
            "outcome": outcome,
            "reason": reason,
        }
    )
    events.append(event)
    kind, title, body, url = compose(entries)
    return Content(
        kind=kind,
        title=title,
        body=body,
        url=url,
        # Twice the documents: enough to cover a document announced, then
        # announced again after a reprocess, without growing forever.
        payload={"documents": entries, "events": events[-2 * MAX_DOCUMENTS :]},
        fresh=fresh,
    )


def compose(entries) -> tuple[str, str, str, str]:
    """(kind, title, body, url) for the documents in one notification."""
    ready = [entry for entry in entries if entry["outcome"] == READY]
    failed = [entry for entry in entries if entry["outcome"] != READY]

    if len(entries) == 1:
        entry = entries[0]
        name = f"“{entry['title']}”"
        if entry["outcome"] == READY:
            return (
                KIND_READY,
                f"{name} is ready",
                "It has been read and indexed. Open it, search it, or ask a "
                "question about it.",
                f"/documents/{entry['id']}",
            )
        return (
            KIND_QUARANTINED if entry["outcome"] == QUARANTINED else KIND_FAILED,
            f"{name} could not be processed: {reason_text(entry['reason'])}",
            _hint(entry),
            f"/documents/{entry['id']}?tab=processing",
        )

    if not failed:
        title = f"{len(ready)} documents are ready"
    elif not ready:
        title = f"{len(failed)} documents could not be processed"
    else:
        verb = "is" if len(ready) == 1 else "are"
        noun = "document" if len(ready) == 1 else "documents"
        title = (
            f"{len(ready)} {noun} {verb} ready, {len(failed)} could not be processed"
        )

    lines = []
    if ready:
        lines.append(f"Ready: {_names(ready)}.")
    if failed:
        lines.append(f"Could not be processed: {_names(failed, reasons=True)}.")

    if not failed:
        url = "/documents"
    elif not ready and all(entry["outcome"] == FAILED for entry in failed):
        url = "/documents?status=FAILED"
    else:
        url = "/documents"
    return (KIND_FAILED if failed else KIND_READY, title, "\n".join(lines), url)


def reason_text(code: str) -> str:
    return PLAIN_REASON.get(code, UNKNOWN_REASON)


def _hint(entry) -> str:
    if entry["outcome"] == QUARANTINED:
        return (
            "The file was quarantined and will not be processed again. If you "
            "know it is safe, upload a clean copy as a new version."
        )
    if entry["reason"] in NOT_THE_FILE:
        return (
            "This was not caused by the file itself. Reprocess it from the "
            "document page, or ask an administrator to retry it."
        )
    return (
        "Open it to see which step failed. Uploading a new version of the "
        "file starts again."
    )


def _names(entries, *, reasons=False) -> str:
    named = [
        f"{entry['title']} ({reason_text(entry['reason'])})"
        if reasons
        else entry["title"]
        for entry in entries[:NAMED]
    ]
    rest = len(entries) - len(named)
    if rest:
        return f"{', '.join(named)} and {rest} more"
    if len(named) == 1:
        return named[0]
    return f"{', '.join(named[:-1])} and {named[-1]}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
