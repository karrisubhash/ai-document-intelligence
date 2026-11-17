"""Build and store the brief of one extraction pass. The ORM boundary.

Reads only what is stored - page text, tables, chunks and their vectors,
extracted fields - never the original upload, and never calls a language
model. Everything it writes is keyed to the DocumentContent it read, so a
reprocess produces a new brief beside the old pass rather than rewriting
the one an earlier reader saw.
"""

import contextlib
import hashlib
import logging
import math
import re
import time

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F

from documents.models import (
    Document,
    DocumentCentroid,
    DocumentChunk,
    DocumentEntity,
    DocumentInsight,
    DocumentVersion,
    ExtractedField,
)
from documents.models.chunk import EMBEDDING_DIMENSIONS
from documents.processing.tables import text_rows

from . import GENERATOR_NAME, GENERATOR_VERSION
from .entities import extract_entities
from .questions import candidates as question_candidates
from .questions import choose as choose_questions
from .stats import compute_stats
from .summary import key_phrases, key_points, summarize
from .text import STOPWORDS, WORD, collapse, normalize_term, segment

logger = logging.getLogger("adi.insights")

# Below this many live documents, corpus document frequency says nothing a
# reader would recognise, and the brief uses within-document statistics.
CORPUS_MIN_DOCUMENTS = 20
CORPUS_CACHE_SECONDS = 6 * 60 * 60
# Live chunks, at most 20,000 of them: document frequency over a sample is
# as good a rarity signal as over everything, at a bounded cost.
CORPUS_SAMPLE_SQL = (
    "SELECT c.search_vector FROM documents_documentchunk c "
    "JOIN documents_documentversion v ON v.current_content_id = c.document_content_id "
    "WHERE c.search_vector IS NOT NULL LIMIT 20000"
)
CORPUS_COUNT_SQL = (
    "SELECT count(*) FROM (SELECT 1 FROM documents_documentchunk c "
    "JOIN documents_documentversion v ON v.current_content_id = c.document_content_id "
    "WHERE c.search_vector IS NOT NULL LIMIT 20000) AS s"
)
# Advisory-lock namespace, distinct from chunking's (7301): one enrichment
# per extraction pass at a time, without ever blocking indexing.
LOCK_NAMESPACE = 7302
# Words a templated question adds that the answer's passage will not hold.
QUESTION_FILLER = frozenset(
    "what when who which how many much does document say says said cover covers "
    "worth taught apply applies due issued approved take effect review".split()
)
SAFE_TERM = re.compile(r"[^a-z0-9]")


@contextlib.contextmanager
def content_lock(content_id: int):
    key = int(content_id) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [LOCK_NAMESPACE, key])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, key])


def live_versions():
    """Versions whose live pass is what readers see: current, not withdrawn."""
    return DocumentVersion.objects.filter(
        document__current_version=F("pk"), current_content__isnull=False
    )


# --- corpus statistics -------------------------------------------------------


def _corpus_document_frequency():
    """{lexeme: chunks containing it} over a sample of live chunks, cached."""
    documents = live_versions().count()
    if documents < CORPUS_MIN_DOCUMENTS:
        return None
    key = f"insights:corpus-df:v1:{documents // 50}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    with connection.cursor() as cursor:
        cursor.execute(CORPUS_COUNT_SQL)
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT word, ndoc FROM ts_stat(%s) WHERE ndoc > 1", [CORPUS_SAMPLE_SQL]
        )
        frequency = dict(cursor.fetchall())
    data = {"chunks": total, "df": frequency}
    cache.set(key, data, CORPUS_CACHE_SECONDS)
    return data


def corpus_idf_function(pages):
    """An idf(term) for this document's terms, or None for a small corpus."""
    statistics = _corpus_document_frequency()
    if not statistics or not statistics["chunks"]:
        return None

    terms = set()
    for _, text in pages:
        for word in WORD.findall(text or ""):
            lowered = word.lower()
            if len(lowered) >= 3 and lowered not in STOPWORDS:
                terms.add(normalize_term(lowered))
    terms = sorted(terms)[:20_000]
    if not terms:
        return None

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT t, to_tsvector('english', t)::text FROM unnest(%s::text[]) AS t",
            [terms],
        )
        lexemes = {}
        for term, vector in cursor.fetchall():
            match = re.match(r"'([^']+)'", vector or "")
            if match:
                lexemes[term] = match.group(1)

    total = statistics["chunks"]
    frequency = statistics["df"]
    ceiling = math.log(total + 1) + 1.0

    def idf(term):
        lexeme = lexemes.get(term)
        if lexeme is None:
            return ceiling
        return math.log((total + 1) / (frequency.get(lexeme, 0) + 1)) + 1.0

    return idf


# --- semantic signal ---------------------------------------------------------


def _semantic_scorer(content):
    """Sentence-to-centroid similarity, when this pass has a semantic centroid
    and the configured provider is the model that made it. Otherwise None."""
    from documents.processing.chunking.exceptions import (
        EmbeddingProviderNotConfigured,
        EmbeddingProviderUnavailable,
    )
    from documents.processing.chunking.service import get_provider

    try:
        provider = get_provider()
    except (EmbeddingProviderNotConfigured, EmbeddingProviderUnavailable):
        return None
    if not provider.semantic:
        return None
    centroid = DocumentCentroid.objects.filter(
        document_content=content,
        model_name=provider.name,
        model_version=provider.version,
        semantic=True,
    ).first()
    if centroid is None:
        return None
    vector = [float(value) for value in centroid.embedding]

    def score(segments):
        try:
            batch = provider.embed([s.text for s in segments])
        except Exception as exc:
            logger.warning("insights.semantic_unavailable", extra={"error": str(exc)})
            return None
        return [
            sum(a * b for a, b in zip(v, vector, strict=True)) for v in batch.vectors
        ]

    return score


# --- centroid ----------------------------------------------------------------


def compute_centroid(content):
    """Store the mean of this pass's chunk vectors, per embedding model.

    Returns the centroid for the configured provider's model when there is
    one, else any; None when the pass has no vectors at all.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.model_name, e.model_version, bool_and(e.semantic),
                   count(*), AVG(e.embedding)::text
            FROM documents_documentchunkembedding e
            JOIN documents_documentchunk c ON c.id = e.chunk_id
            WHERE c.document_content_id = %s
            GROUP BY e.model_name, e.model_version
            """,
            [content.id],
        )
        rows = cursor.fetchall()

    stored = []
    for model_name, model_version, semantic, count, text in rows:
        values = [float(value) for value in text.strip("[]").split(",") if value]
        norm = math.sqrt(sum(value * value for value in values))
        if len(values) != EMBEDDING_DIMENSIONS or norm == 0.0:
            continue
        centroid, _ = DocumentCentroid.objects.update_or_create(
            document_content=content,
            model_name=model_name,
            model_version=model_version,
            defaults={
                "document_version": content.document_version,
                "dimensions": len(values),
                "semantic": bool(semantic),
                "chunk_count": count,
                "embedding": [value / norm for value in values],
            },
        )
        stored.append(centroid)
    return stored


# --- fields and self-check ---------------------------------------------------


def current_fields(content, document_type):
    """FOUND fields of the newest extraction of this pass under its type."""
    result = (
        content.extraction_results.filter(document_type=document_type)
        .order_by("-created_at")
        .first()
    )
    if result is None:
        return []
    return list(
        result.extracted_fields.filter(status=ExtractedField.Status.FOUND)
        .order_by("page_number", "character_start")
        .values_list(
            "field_name", "value", "page_number", "character_start", "character_end"
        )
    )


def make_checker(content):
    """Would this question retrieve its answer from this document's chunks?

    A lexical OR query over the question's content words, against this
    pass's own indexed chunks: the question passes when the chunk holding
    its anchor ranks in the top three (or, with no anchor, when anything
    matches at all). Cheap - one indexed query - and conservative.
    """
    chunks = DocumentChunk.objects.filter(document_content=content)

    def check(question, anchor):
        terms = []
        for word in WORD.findall(question):
            lowered = SAFE_TERM.sub("", word.lower())
            if len(lowered) < 3 or lowered in STOPWORDS or lowered in QUESTION_FILLER:
                continue
            terms.append(lowered)
        if not terms:
            return False
        query = SearchQuery(
            " | ".join(sorted(set(terms))), search_type="raw", config="english"
        )
        top = list(
            chunks.filter(search_vector=query)
            .annotate(rank=SearchRank(F("search_vector"), query))
            .order_by("-rank", "chunk_index")
            .values_list("page_number", "character_start", "character_end")[:3]
        )
        if anchor is None:
            return bool(top)
        page, offset = anchor
        return any(p == page and s <= offset < e for p, s, e in top)

    return check


# --- verification and persistence --------------------------------------------


def _verified(items, page_texts, what):
    kept = []
    for item in items:
        text = page_texts.get(item["page_number"])
        if text is None or text[item["start"] : item["end"]] != item["text"]:
            # Never expected - every span is cut from these very strings -
            # and never stored if it happens: a span that does not re-slice
            # is not a citation.
            logger.error(
                "insights.span_rejected",
                extra={"what": what, "page": item["page_number"]},
            )
            continue
        kept.append(item)
    return kept


def _tables(content, text_pages) -> tuple:
    """(regions, question material) of the pass's tables.

    Regions are (page, start, end), kept out of the summary and key points:
    a row is not a sentence about the document. Question material is
    (labels, rows of values, page, start) per table, for "What is the <column>
    for <row>?". Since ingestion-v4 both come from the labelled rows in the
    page text; a pass before that stored its tables as grids.
    """
    stored = list(
        content.tables.order_by("table_index").values_list(
            "page_number", "character_start", "character_end", "header", "rows"
        )
    )
    if stored:
        return (
            [(page, start, end) for page, start, end, _, _ in stored],
            [
                (
                    header or (rows[0] if rows else []),
                    rows[1:] if not header else rows,
                    page,
                    start,
                )
                for page, start, _, header, rows in stored
            ],
        )

    regions = []
    questions = []
    asked = set()
    for page, text in text_pages:
        for region in text_rows.regions(text):
            regions.append((page, region.start, region.end))
            if region.number in asked:
                continue
            lines = text_rows.row_lines(text[region.start : region.end])
            labels = [label for label, _ in lines[0].pairs] if lines else []
            if sum(1 for label in labels if label) < 1:
                continue
            asked.add(region.number)
            rows = [
                [value for _, value in line.pairs]
                for line in lines
                if [label for label, _ in line.pairs] == labels
            ]
            questions.append((labels, rows, page, region.start))
    return regions, questions


def build_insight(content, *, processing_job=None) -> DocumentInsight:
    """Compute and store the brief of one extraction pass. Idempotent."""
    with content_lock(content.id):
        return _build(content, processing_job=processing_job)


def _build(content, *, processing_job=None):
    started = time.monotonic()
    version = content.document_version
    document_type = (
        Document.objects.filter(pk=version.document_id)
        .values_list("document_type", flat=True)
        .first()
    )
    pages = list(
        content.pages.order_by("page_number").values_list(
            "page_number", "text", "extraction_method"
        )
    )
    text_pages = [(number, text or "") for number, text, _ in pages]
    page_texts = dict(text_pages)
    table_regions, table_questions = _tables(content, text_pages)

    entities = extract_entities(text_pages)
    segments = segment(text_pages)
    idf = corpus_idf_function(text_pages)
    summary, info = summarize(
        segments,
        excluded_regions=table_regions,
        corpus_idf=idf,
        semantic=_semantic_scorer(content),
    )
    points = key_points(
        segments,
        entities,
        summary_positions={segment.position for segment, _ in summary},
        excluded_regions=table_regions,
        scorer=info["scorer"] if info else None,
    )
    phrases = key_phrases(text_pages, corpus_idf=idf)
    stats = compute_stats(pages)

    fields = current_fields(content, document_type)
    deadlines = [e for e in entities if e.entity_type == "DEADLINE"]
    headings = [s for s in segments if s.is_heading and s.position > 0]
    candidate_questions = question_candidates(
        document_type=document_type,
        fields=fields,
        deadlines=deadlines,
        tables=table_questions,
        headings=headings,
        phrases=phrases,
    )
    questions, checks = choose_questions(candidate_questions, make_checker(content))

    summary_items = _verified(
        [
            {
                "text": s.text,
                "page_number": s.page_number,
                "start": s.start,
                "end": s.end,
                "score": score,
            }
            for s, score in summary
        ],
        page_texts,
        "summary",
    )
    point_items = _verified(
        [
            {
                "text": s.text,
                "page_number": s.page_number,
                "start": s.start,
                "end": s.end,
                "kind": kind,
            }
            for s, kind in points
        ],
        page_texts,
        "key_point",
    )
    entity_rows = [
        e
        for e in entities
        if page_texts.get(e.page_number, "")[e.start : e.end] == e.text and e.text
    ]

    full_text = "\n".join(text for _, text in text_pages)
    metadata = {
        "duration_seconds": round(time.monotonic() - started, 3),
        "sentences": len(segments),
        "summary_candidates": info["candidates"] if info else 0,
        "signals": ["tfidf"]
        + (["corpus_idf"] if idf else [])
        + (["embeddings"] if info and info["semantic"] else []),
        "question_candidates": len(candidate_questions),
        "question_checks": checks,
        "fields_used": len(fields),
        "document_type": document_type,
        # Identical text, whatever the file: the lexical near-duplicate test.
        "fingerprint": hashlib.sha256(collapse(full_text).lower().encode()).hexdigest(),
    }

    with transaction.atomic():
        insight, _ = DocumentInsight.objects.update_or_create(
            document_content=content,
            defaults={
                "document_version": version,
                "processing_job": processing_job or content.processing_job,
                "status": DocumentInsight.Status.READY,
                "generator_name": GENERATOR_NAME,
                "generator_version": GENERATOR_VERSION,
                "summary": summary_items,
                "key_points": point_items,
                "key_phrases": [
                    {"text": text, "score": score} for text, score in phrases
                ],
                "stats": stats,
                "suggested_questions": questions,
                "metadata": metadata,
                "error_message": "",
            },
        )
        insight.entities.all().delete()
        DocumentEntity.objects.bulk_create(
            [
                DocumentEntity(
                    insight=insight,
                    document_version=version,
                    entity_type=e.entity_type,
                    text=e.text,
                    normalized=(e.normalized or "")[:255],
                    value_date=e.value_date,
                    label=e.label[:200],
                    page_number=e.page_number,
                    character_start=e.start,
                    character_end=e.end,
                    occurrences=e.occurrences,
                    position=e.position,
                )
                for e in entity_rows
            ]
        )
    return insight


def record_failure(content, exc, *, processing_job=None):
    """Say the brief failed - unless a good one for this pass already exists."""
    existing = DocumentInsight.objects.filter(document_content=content).first()
    if existing is not None and existing.status == DocumentInsight.Status.READY:
        return existing
    insight, _ = DocumentInsight.objects.update_or_create(
        document_content=content,
        defaults={
            "document_version": content.document_version,
            "processing_job": processing_job or content.processing_job,
            "status": DocumentInsight.Status.FAILED,
            "generator_name": GENERATOR_NAME,
            "generator_version": GENERATOR_VERSION,
            "error_message": f"{type(exc).__name__}: {exc}"[:2000],
        },
    )
    return insight
