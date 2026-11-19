"""Tell the platform a long stage is still working.

A job is declared STALLED (`recover_stalled_jobs`, and Reprocess) when its row
has not been touched for STALLED_JOB_MINUTES, and the pipeline touches the row
once per STAGE. That was fine while a stage lasted minutes. With no ceiling on
pages, OCR or embedding one long document can take hours, and a job that is
working - and must not be run a second time beside itself - would be marked
STALLED and replaced.

`beat` is called from inside the loops that can run that long: it touches the
job's `updated_at`, at most once every `every` seconds.
"""

import logging
import time

from django.utils import timezone

logger = logging.getLogger("adi.heartbeat")

EVERY_SECONDS = 30.0


def beat(job, *, every: float = EVERY_SECONDS) -> bool:
    """Touch `job`'s updated_at unless that was done lately; True if it was.

    `job` is a ProcessingJob or None (a stage run outside a job, as the
    re-index does, has nothing to touch). A failure is logged and swallowed:
    a heartbeat that cannot be written must not fail the document it was
    only reporting on.
    """
    if job is None or getattr(job, "pk", None) is None:
        return False

    now = time.monotonic()
    last = getattr(job, "_last_heartbeat", None)
    if last is not None and now - last < every:
        return False
    job._last_heartbeat = now

    from documents.models import ProcessingJob

    try:
        ProcessingJob.objects.filter(pk=job.pk).update(updated_at=timezone.now())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "heartbeat.failed", extra={"job_id": job.pk, "error": type(exc).__name__}
        )
        return False
    return True
