"""Post-publish enrichment of one version's live extraction pass.

Runs on the `index` queue after a job has COMPLETED (and again after a
person changes the type, and from `manage.py build_insights`). Four steps,
each independent and each allowed to fail without stopping the others:

1. Fields under the current type. The newest extraction of the live pass
   must have been run under the document's current type; when it was not -
   the type was changed by a person, or the document predates the extractor
   being on - extraction runs again. Convergent rather than event-driven, so
   any number of redeliveries and races end in the same state.
2. The related-documents centroid (mean chunk vector per model).
3. The brief (summary, key points, entities, key phrases, stats, questions).
4. The first-page thumbnail, when the version has none.

Recorded as one INSIGHTS event on the job that produced the pass, keyed by
the Celery task id so a redelivered message updates its event instead of
adding a second. Never touches the document's status: enrichment can fail,
the document is still processed.
"""

import logging
import time

from django.conf import settings
from django.utils import timezone

from documents.models import (
    DocumentThumbnail,
    DocumentVersion,
    ProcessingEvent,
    ProcessingJob,
)
from documents.services import is_quarantined

from . import service, thumbnails

logger = logging.getLogger("adi.insights")


class Reason:
    PUBLISH = "publish"
    TYPE_CHANGED = "type_changed"
    BACKFILL = "backfill"


def ensure_extraction(content, document_type) -> str:
    """Re-run field extraction when the live pass was extracted under
    another type (or never). Returns what happened, for the record."""
    from documents.processing.field_extraction import extract
    from documents.processing.field_extraction.exceptions import (
        ExtractorNotConfigured,
        FieldExtractionError,
        NoSchemaForDocumentType,
    )

    latest = content.extraction_results.order_by("-created_at").first()
    if latest is not None and latest.document_type == document_type:
        return "current"
    try:
        extract(content.id)
    except ExtractorNotConfigured:
        return "not_configured"
    except NoSchemaForDocumentType:
        return "no_schema"
    except FieldExtractionError as exc:
        logger.warning(
            "insights.extraction_failed",
            extra={"content_id": content.id, "error": str(exc)},
        )
        return "failed"
    return "extracted"


def _event(job, task_id):
    if job is None:
        return None
    if task_id:
        existing = ProcessingEvent.objects.filter(
            processing_job=job,
            stage=ProcessingJob.Stage.INSIGHTS,
            metadata__task_id=task_id,
        ).first()
        if existing is not None:
            existing.status = ProcessingEvent.Status.STARTED
            existing.started_at = timezone.now()
            existing.save(update_fields=["status", "started_at"])
            return existing
    return ProcessingEvent.objects.create(
        processing_job=job,
        stage=ProcessingJob.Stage.INSIGHTS,
        status=ProcessingEvent.Status.STARTED,
        started_at=timezone.now(),
        metadata={"task_id": task_id} if task_id else {},
    )


def enrich_version(version_id: int, *, reason=Reason.PUBLISH, task_id="") -> dict:
    """Enrich one version's live pass. Never raises for a document problem."""
    if not getattr(settings, "DOCUMENT_INSIGHTS_ENABLED", True):
        return {"skipped": "disabled"}

    version = (
        DocumentVersion.objects.select_related("document", "current_content")
        .filter(pk=version_id)
        .first()
    )
    if version is None:
        return {"skipped": "missing"}
    document = version.document
    if document.current_version_id != version.pk:
        # Its output would never be read: a newer version is the live one.
        return {"skipped": "superseded"}
    content = version.current_content
    if content is None or is_quarantined(version):
        return {"skipped": "no_live_content"}

    started = time.monotonic()
    event = _event(content.processing_job, task_id)
    steps = {}

    try:
        steps["fields"] = ensure_extraction(content, document.document_type)
    except Exception:
        logger.exception("insights.fields_failed", extra={"content_id": content.id})
        steps["fields"] = "error"

    try:
        centroids = service.compute_centroid(content)
        steps["centroid"] = (
            ("semantic" if any(c.semantic for c in centroids) else "lexical")
            if centroids
            else "no_vectors"
        )
    except Exception:
        logger.exception("insights.centroid_failed", extra={"content_id": content.id})
        steps["centroid"] = "error"

    insight = None
    try:
        insight = service.build_insight(content)
        steps["brief"] = "ready"
    except Exception as exc:
        logger.exception("insights.brief_failed", extra={"content_id": content.id})
        service.record_failure(content, exc)
        steps["brief"] = "error"

    file_type = (content.metadata or {}).get("file_type", "")
    if DocumentThumbnail.objects.filter(document_version=version).exists():
        steps["thumbnail"] = "exists"
    else:
        try:
            thumbnails.render_thumbnail(version, file_type)
            steps["thumbnail"] = "rendered"
        except thumbnails.NoThumbnail:
            steps["thumbnail"] = "not_applicable"
        except Exception:
            logger.exception(
                "insights.thumbnail_failed", extra={"version_id": version.pk}
            )
            steps["thumbnail"] = "error"

    duration = round(time.monotonic() - started, 3)
    if event is not None:
        failed = steps.get("brief") == "error"
        event.status = (
            ProcessingEvent.Status.FAILED
            if failed
            else ProcessingEvent.Status.SUCCEEDED
        )
        event.message = _describe(steps, insight, reason)
        event.metadata = {
            **(event.metadata or {}),
            "reason": reason,
            "steps": steps,
            "duration_seconds": duration,
        }
        event.completed_at = timezone.now()
        event.save(update_fields=["status", "message", "metadata", "completed_at"])

    return {"content_id": content.id, "steps": steps, "duration_seconds": duration}


def _describe(steps, insight, reason) -> str:
    if steps.get("brief") == "error":
        text = "The brief could not be prepared; the document itself is unaffected"
    else:
        parts = []
        if insight is not None:
            parts.append(f"{len(insight.summary)} summary sentence(s)")
            parts.append(f"{len(insight.key_points)} key point(s)")
            parts.append(f"{insight.entities.count()} key fact(s)")
            parts.append(f"{len(insight.suggested_questions)} suggested question(s)")
        text = "Brief ready: " + ", ".join(parts)
    if reason == Reason.TYPE_CHANGED:
        text += "; refreshed after the type was changed"
    if steps.get("fields") == "extracted":
        text += "; fields extracted for the current type"
    return text
