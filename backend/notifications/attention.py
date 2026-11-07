"""What is waiting for an administrator: the counts behind the sidebar badges.

    pending_signups        sign-up requests this administrator may decide
                           (accounts.permissions.reviewable_signups)
    failed_documents       documents in their scope whose processing failed,
                           quarantined ones included - what the Processing
                           screen's "Failed" view lists runs for
    quarantined_documents  how many of those the security scan quarantined
    stalled_jobs           runs in their scope that stopped reporting
                           progress (services.stalled_cutoff), the
                           Processing screen's "Stalled" view

Scoped like the operations views (operations.views._department_scope and
JobListView): a system administrator counts everything, a department
administrator their department's documents. Archived documents are not
counted - archiving a broken document is one way of dealing with it.

Every open tab polls this, so each person's answer is cached for
NOTIFICATIONS_ATTENTION_CACHE_SECONDS. A version number in the cache key
is bumped after any sign-up request is saved or a run is created or
finishes, so a decision or a retry shows on the next poll instead of up to
half a minute later; deleting or archiving a failed document waits for the
cache to expire.
"""

import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count

from accounts.models import SignupRequest
from accounts.permissions import reviewable_signups
from documents import services
from documents.authorization import get_profile, is_system_admin
from documents.models import Document, ProcessingJob

logger = logging.getLogger("adi.notifications")

VERSION_KEY = "notifications:attention:version"


def attention_for(user) -> dict:
    key = f"notifications:attention:{_version()}:{user.pk}"
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    counts = compute(user)
    try:
        cache.set(key, counts, _ttl())
    except Exception:
        logger.warning("notifications.attention_cache_unavailable")
    return counts


def compute(user) -> dict:
    department = None if is_system_admin(user) else get_profile(user).department

    pending = reviewable_signups(
        user, SignupRequest.objects.filter(status=SignupRequest.Status.PENDING)
    ).count()

    broken = Document.objects.filter(
        archived_at__isnull=True,
        status__in=(Document.Status.FAILED, Document.Status.QUARANTINED),
    )
    stalled = ProcessingJob.objects.filter(
        status=ProcessingJob.Status.PROCESSING,
        updated_at__lt=services.stalled_cutoff(),
    )
    if department is not None:
        broken = broken.filter(department=department)
        stalled = stalled.filter(document_version__document__department=department)

    by_status = dict(
        broken.values_list("status")
        .annotate(total=Count("id"))
        .values_list("status", "total")
    )
    quarantined = by_status.get(Document.Status.QUARANTINED, 0)
    return {
        "pending_signups": pending,
        "failed_documents": by_status.get(Document.Status.FAILED, 0) + quarantined,
        "quarantined_documents": quarantined,
        "stalled_jobs": stalled.count(),
    }


def invalidate() -> None:
    """Make every cached answer stale. After the commit, so a request that
    computes in between cannot cache the state from before it."""
    transaction.on_commit(_bump)


def job_saved(sender, instance, created, update_fields=None, **kwargs):
    if created or (
        instance.status in (ProcessingJob.Status.COMPLETED, ProcessingJob.Status.FAILED)
        and (update_fields is None or "status" in update_fields)
    ):
        invalidate()


def signup_saved(sender, instance, **kwargs):
    invalidate()


def _bump() -> None:
    try:
        cache.incr(VERSION_KEY)
    except ValueError:
        # Not there (first use, or evicted): start from the clock, so no
        # number used before can come back and revive old entries.
        _start_version()
    except Exception:
        logger.warning("notifications.attention_cache_unavailable")


def _version() -> int:
    try:
        value = cache.get(VERSION_KEY)
        if value is None:
            value = _start_version()
        return int(value)
    except Exception:
        return 0


def _start_version() -> int:
    value = time.time_ns()
    cache.add(VERSION_KEY, value, None)
    return cache.get(VERSION_KEY) or value


def _ttl() -> int:
    return getattr(settings, "NOTIFICATIONS_ATTENTION_CACHE_SECONDS", 30)
