"""Read models for the document brief, related documents, suggestions and
deadlines. Every function here takes the requesting user, and every
cross-document query starts from `authorization.visible_documents` - a
filter can only ever narrow it. Response shapes are documented in
docs/api/reference.md ("Document intelligence").
"""

import datetime
import hashlib

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Case, F, IntegerField, Value, When
from django.utils import timezone
from pgvector.django import CosineDistance

from documents import authorization
from documents.models import (
    Document,
    DocumentCentroid,
    DocumentEntity,
    DocumentInsight,
    DocumentThumbnail,
)
from documents.processing.insights import METHOD
from documents.processing.insights.text import collapse
from documents.services import is_quarantined

IN_FLIGHT = (
    Document.Status.UPLOADED,
    Document.Status.QUEUED,
    Document.Status.PROCESSING,
)
RELATED_LIMIT = 5
# Weighted key-phrase overlap below this is not "related" (lexical method).
LEXICAL_MIN_SIMILARITY = 0.2
LEXICAL_PHRASES = 12
LEXICAL_SHORTLIST = 30
SUGGESTIONS_LIMIT = 8
SUGGESTION_DOCUMENTS = 40
DEADLINES_LIMIT = 20
DEADLINE_DAYS_MAX = 366

EMPTY_STATS = {
    "pages": 0,
    "words": 0,
    "reading_minutes": 0,
    "ocr_pages": 0,
    "language": "unknown",
}


def _live_content(version):
    if version is None or is_quarantined(version):
        return None
    return version.current_content


def _document_type(document):
    return {
        "value": document.document_type,
        "source": document.document_type_source,
        "confidence": document.type_confidence,
    }


def _ai(insight):
    if insight is None or insight.ai_generated_at is None:
        return None
    return {
        "summary": insight.ai_summary,
        "questions": insight.ai_questions or [],
        "model": insight.ai_model,
        "generated_at": insight.ai_generated_at,
    }


def insight_payload(document) -> dict:
    """GET /documents/{id}/insights/ - the brief of the live extraction pass."""
    version = document.current_version
    content = _live_content(version)
    insight = (
        DocumentInsight.objects.filter(document_content=content).first()
        if content is not None
        else None
    )

    if insight is not None:
        status = insight.status
    elif content is not None:
        enabled = getattr(settings, "DOCUMENT_INSIGHTS_ENABLED", True)
        status = "PENDING" if enabled else "UNAVAILABLE"
    elif version is not None and document.status in IN_FLIGHT:
        status = "PENDING"
    else:
        status = "UNAVAILABLE"

    ready = insight is not None and insight.status == DocumentInsight.Status.READY
    entities = []
    if ready:
        entities = [
            {
                "type": entity.entity_type,
                "text": entity.text,
                "normalized": entity.normalized or None,
                "page_number": entity.page_number,
                "start": entity.character_start,
                "end": entity.character_end,
                "count": entity.occurrences,
                # Additive to the contract: what a date or deadline is for,
                # in the document's words ("" when there is nothing to say).
                "label": entity.label,
            }
            for entity in insight.entities.all()
        ]

    return {
        "document_id": document.pk,
        "version_id": version.pk if version is not None else None,
        "content_id": content.pk if content is not None else None,
        "status": status,
        "method": METHOD,
        "generated_at": insight.updated_at if ready else None,
        "summary": [
            {key: item[key] for key in ("text", "page_number", "start", "end")}
            for item in (insight.summary if ready else [])
        ],
        "key_points": [
            {key: item[key] for key in ("text", "page_number", "start", "end", "kind")}
            for item in (insight.key_points if ready else [])
        ],
        "key_phrases": insight.key_phrases if ready else [],
        "entities": entities,
        "stats": {**EMPTY_STATS, **(insight.stats if ready else {})},
        "suggested_questions": insight.suggested_questions if ready else [],
        "document_type": _document_type(document),
        "ai": _ai(insight) if ready else None,
    }


# --- related documents ---------------------------------------------------------


def _candidates(user, document):
    """Readable, live, non-archived documents other than this one."""
    return authorization.visible_documents(
        user,
        Document.objects.filter(
            archived_at__isnull=True,
            current_version__current_content__isnull=False,
        ).exclude(pk=document.pk),
    )


def _fingerprint(content):
    insight = DocumentInsight.objects.filter(document_content=content).first()
    return (insight.metadata or {}).get("fingerprint") if insight else None


def _row(document, similarity, near_duplicate):
    return {
        "id": document.pk,
        "title": document.title,
        "document_type": document.document_type,
        "department": document.department,
        "updated_at": document.updated_at,
        "similarity": round(float(similarity), 4),
        "near_duplicate": bool(near_duplicate),
    }


def _exact_twins(document, version, content, candidates):
    """Ids of candidates holding the same file or the same text."""
    twins = set(
        candidates.filter(current_version__checksum=version.checksum)
        .exclude(current_version__checksum="")
        .values_list("pk", flat=True)
    )
    fingerprint = _fingerprint(content)
    if fingerprint:
        twins |= set(
            candidates.filter(
                current_version__current_content__insight__metadata__fingerprint=(
                    fingerprint
                )
            ).values_list("pk", flat=True)
        )
    return twins


def related_documents(user, document):
    """(results, method). method: semantic | lexical | unavailable."""
    version = document.current_version
    content = _live_content(version)
    if content is None:
        return [], "unavailable"

    candidates = _candidates(user, document)
    twins = _exact_twins(document, version, content, candidates)
    threshold = getattr(settings, "DOCUMENT_NEAR_DUPLICATE_SIMILARITY", 0.97)

    centroid = (
        DocumentCentroid.objects.filter(document_content=content, semantic=True)
        .order_by("-created_at")
        .first()
    )
    if centroid is not None:
        minimum = getattr(settings, "DOCUMENT_RELATED_MIN_SIMILARITY", 0.5)
        with transaction.atomic():
            # More candidates per index probe: the authorization filter is
            # applied after the approximate search, and the default 40 could
            # return fewer than five readable neighbours.
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL hnsw.ef_search = 200")
            rows = list(
                DocumentCentroid.objects.filter(
                    model_name=centroid.model_name,
                    model_version=centroid.model_version,
                    document_version__document__in=candidates.values("pk"),
                    document_version=F("document_version__document__current_version"),
                    document_content=F("document_version__current_content"),
                )
                .annotate(distance=CosineDistance("embedding", centroid.embedding))
                .select_related("document_version__document")
                .order_by("distance")[: RELATED_LIMIT * 3]
            )
        results, seen = [], set()
        for row in rows:
            related = row.document_version.document
            similarity = max(0.0, 1.0 - float(row.distance))
            if related.pk in seen or (similarity < minimum and related.pk not in twins):
                continue
            seen.add(related.pk)
            results.append(
                _row(
                    related,
                    similarity,
                    similarity >= threshold or related.pk in twins,
                )
            )
        return results[:RELATED_LIMIT], "semantic"

    return _lexical_related(document, content, candidates, twins), "lexical"


def _lexical_related(document, content, candidates, twins):
    """Weighted key-phrase overlap, both ways, when vectors are not semantic.

    Forward: how much of this document's key phrases each candidate's live
    chunks contain (an indexed full-text query). Reverse: how much of the
    candidate's key phrases this document's text contains. Their harmonic
    mean is symmetric and does not let one long document be "related" to
    everything because it mentions everything.
    """
    insight = DocumentInsight.objects.filter(
        document_content=content, status=DocumentInsight.Status.READY
    ).first()
    phrases = (insight.key_phrases if insight else [])[:LEXICAL_PHRASES]
    if not phrases:
        return []

    live = dict(candidates.values_list("current_version__current_content_id", "pk"))
    if not live:
        return []

    texts = [phrase["text"] for phrase in phrases]
    weights = [max(float(phrase.get("score", 0.0)), 0.01) for phrase in phrases]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.document_content_id, q.idx
            FROM documents_documentchunk c
            JOIN unnest(%s::text[]) WITH ORDINALITY AS q(phrase, idx)
              ON c.search_vector @@ phraseto_tsquery('english', q.phrase)
            WHERE c.document_content_id = ANY(%s)
            GROUP BY c.document_content_id, q.idx
            """,
            [texts, list(live)],
        )
        matches = cursor.fetchall()

    total = sum(weights)
    forward = {}
    for content_id, index in matches:
        forward[content_id] = forward.get(content_id, 0.0) + weights[index - 1] / total
    shortlist = sorted(forward, key=forward.get, reverse=True)[:LEXICAL_SHORTLIST]
    if not shortlist:
        return []

    own_text = collapse(
        " ".join(content.pages.order_by("page_number").values_list("text", flat=True))
    ).lower()
    others = {
        row.document_content_id: row.key_phrases
        for row in DocumentInsight.objects.filter(
            document_content_id__in=shortlist, status=DocumentInsight.Status.READY
        ).only("document_content_id", "key_phrases")
    }
    documents = {
        d.pk: d for d in Document.objects.filter(pk__in=[live[c] for c in shortlist])
    }

    scored = []
    for content_id in shortlist:
        their = others.get(content_id) or []
        their_total = sum(max(float(p.get("score", 0.0)), 0.01) for p in their)
        reverse = (
            sum(
                max(float(p.get("score", 0.0)), 0.01)
                for p in their
                if p["text"].lower() in own_text
            )
            / their_total
            if their_total
            else 0.0
        )
        f = forward[content_id]
        similarity = 2 * f * reverse / (f + reverse) if f + reverse else 0.0
        document_id = live[content_id]
        if similarity < LEXICAL_MIN_SIMILARITY and document_id not in twins:
            continue
        scored.append((similarity, document_id))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        _row(
            documents[document_id],
            1.0 if document_id in twins else similarity,
            document_id in twins,
        )
        for similarity, document_id in scored[:RELATED_LIMIT]
    ]


# --- corpus suggestions ----------------------------------------------------------


def _suggestion_cache_key(user):
    profile = authorization.get_profile(user)
    # Keyed on what decides visibility, so a change of department or role
    # is a different key, not ten minutes of the old department's questions.
    # Hashed: a department name is free text, and not a safe key.
    scope = hashlib.sha256(
        f"{profile.role}\x00{profile.department}\x00"
        f"{int(authorization.is_system_admin(user))}".encode()
    ).hexdigest()[:24]
    return f"insights:suggestions:v1:{user.pk}:{scope}"


def corpus_suggestions(user):
    """Up to eight questions across the documents the user can read."""
    key = _suggestion_cache_key(user)
    picks = cache.get(key)
    if picks is None:
        documents = list(
            authorization.visible_documents(
                user,
                Document.objects.filter(
                    archived_at__isnull=True,
                    current_version__current_content__insight__status=(
                        DocumentInsight.Status.READY
                    ),
                ),
            )
            .order_by("-updated_at", "-pk")
            .values_list(
                "pk", "current_version__current_content__insight__suggested_questions"
            )[:SUGGESTION_DOCUMENTS]
        )
        picks = []
        # One question per document first, so eight suggestions reach eight
        # documents; a second round only when there are fewer documents.
        for round_index in range(3):
            for document_id, questions in documents:
                if len(picks) >= SUGGESTIONS_LIMIT:
                    break
                if questions and len(questions) > round_index:
                    picks.append((questions[round_index], document_id))
        cache.set(
            key, picks, getattr(settings, "DOCUMENT_SUGGESTIONS_CACHE_SECONDS", 600)
        )

    # The cache holds a selection, never a permission: what the user may read
    # is decided again, now, so a document deleted or archived since is gone.
    titles = dict(
        authorization.visible_documents(
            user,
            Document.objects.filter(
                pk__in=[document_id for _, document_id in picks],
                archived_at__isnull=True,
            ),
        ).values_list("pk", "title")
    )
    return [
        {
            "question": question,
            "document_id": document_id,
            "document_title": titles[document_id],
        }
        for question, document_id in picks
        if document_id in titles
    ]


# --- deadlines --------------------------------------------------------------------


def parse_days(raw):
    """The `days` window: an integer from 1 to 366, default 90."""
    if raw is None or str(raw).strip() == "":
        return 90
    raw = str(raw).strip()
    if not raw.isdecimal() or len(raw) > 4:
        return None
    value = int(raw)
    return value if 1 <= value <= DEADLINE_DAYS_MAX else None


def upcoming_deadlines(user, days):
    """Dated facts from readable documents, soonest first."""
    today = timezone.localdate()
    until = today + datetime.timedelta(days=days)
    visible = authorization.visible_documents(
        user, Document.objects.filter(archived_at__isnull=True)
    )
    rows = (
        DocumentEntity.objects.filter(
            entity_type__in=(
                DocumentEntity.EntityType.DEADLINE,
                DocumentEntity.EntityType.DATE,
            ),
            value_date__gte=today,
            value_date__lte=until,
            document_version__document__in=visible.values("pk"),
            # Only the live pass of the current version: a superseded
            # version's dates are not the document's dates any more.
            document_version=F("document_version__document__current_version"),
            insight__document_content=F("document_version__current_content"),
            insight__status=DocumentInsight.Status.READY,
        )
        .annotate(
            rank=Case(
                When(entity_type=DocumentEntity.EntityType.DEADLINE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .select_related("document_version__document")
        .order_by("value_date", "rank", "document_version__document_id", "position")
    )

    results, seen = [], set()
    for entity in rows[: DEADLINES_LIMIT * 4]:
        document = entity.document_version.document
        key = (document.pk, entity.value_date)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "date": entity.value_date.isoformat(),
                "type": entity.entity_type,
                "label": entity.label,
                "text": entity.text,
                "document_id": document.pk,
                "document_title": document.title,
                "page_number": entity.page_number,
                "start": entity.character_start,
                "end": entity.character_end,
            }
        )
        if len(results) >= DEADLINES_LIMIT:
            break
    return results


# --- thumbnails -------------------------------------------------------------------


def thumbnail_for(document):
    """The current version's thumbnail, or None (absent or quarantined)."""
    version = document.current_version
    if version is None or is_quarantined(version):
        return None
    return DocumentThumbnail.objects.filter(document_version=version).first()


# --- duplicate hint on upload ------------------------------------------------------


def possible_duplicates(user, document, limit=5):
    """Other readable documents holding a file with the same SHA-256."""
    version = document.current_version
    if version is None or not version.checksum:
        return []
    matches = (
        authorization.visible_documents(
            user, Document.objects.filter(versions__checksum=version.checksum)
        )
        .exclude(pk=document.pk)
        .distinct()
        .order_by("-created_at")[:limit]
    )
    return [
        {
            "id": match.pk,
            "title": match.title,
            "status": match.status,
            "is_archived": match.is_archived,
            # Whether the matching file is that document's current version,
            # or only one of its earlier ones.
            "is_current_version": match.current_version_id is not None
            and match.versions.filter(
                pk=match.current_version_id, checksum=version.checksum
            ).exists(),
        }
        for match in matches
    ]
