"""The post-publish enrichment task (brief, centroid, thumbnail).

On the `index` queue, beside re-indexing: it reads stored content and uses
the embedder, never OCR, and it must never sit in front of an upload.
One task per version - never one task for the corpus (see tasks_reindex).
"""

import logging

from celery import shared_task
from django.conf import settings
from django.db import InterfaceError, OperationalError

from documents.processing.insights.enrichment import Reason, enrich_version

logger = logging.getLogger("adi.insights")

MAX_RETRIES = 3


@shared_task(bind=True, max_retries=MAX_RETRIES)
def enrich_document_task(self, version_id: int, reason: str = Reason.PUBLISH):
    """Enrich one version's live extraction pass. Idempotent; see enrichment.

    Only a lost database connection is retried. Everything about the
    document itself is recorded on its INSIGHTS event instead of raised,
    because a retry would fail the same way and the document is fine.
    """
    try:
        return enrich_version(version_id, reason=reason, task_id=self.request.id or "")
    except (OperationalError, InterfaceError) as exc:
        raise self.retry(exc=exc, countdown=5 * 2**self.request.retries) from exc


def enqueue_enrichment(version_id: int, reason: str = Reason.PUBLISH) -> None:
    """Queue enrichment, and never let a broker problem become a failure.

    Called after a job has completed (or after a type change committed):
    the document is already in its final state, and a broker that is down
    costs it a brief, which `manage.py build_insights --missing` restores.
    """
    if not getattr(settings, "DOCUMENT_INSIGHTS_ENABLED", True):
        return
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        enrich_document_task.delay(version_id, reason)
        return
    try:
        enrich_document_task.delay(version_id, reason)
    except Exception as exc:
        logger.warning(
            "insights.enqueue_failed",
            extra={"version_id": version_id, "error": type(exc).__name__},
        )
