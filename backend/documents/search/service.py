"""Hybrid retrieval over indexed chunks.

The order of operations here is the security property, not an optimisation.
The permission filter runs FIRST, narrowing the candidate set to documents
the caller may already read, and only then is similarity computed. Section
14 puts it plainly: "Similarity alone is never an authorization mechanism."
Ranking an unauthorized chunk and then dropping it would still have let its
content influence what was returned, and would leak its existence through
result counts and scores.

Lexical and vector retrieval answer different questions. Vector search finds
passages that are *about* the query; lexical search finds the ones that
literally contain "REG/2024/117". Fusing them is what makes a search robust
to both paraphrase and exact identifiers (section 12.2).
"""

import logging
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F
from pgvector.django import CosineDistance

from documents import authorization
from documents.models import Document, DocumentChunk
from documents.processing.chunking import service as chunking_service
from documents.processing.chunking.exceptions import (
    EmbeddingProviderNotConfigured,
    EmbeddingProviderUnavailable,
)

logger = logging.getLogger("adi.search")

# Reciprocal-rank fusion constant. 60 is the value from the original RRF
# paper and is deliberately large relative to the result depth: it flattens
# the contribution curve so a result ranked 1st by one retriever does not
# automatically outrank something both retrievers agree on at 3rd.
RRF_K = 60

# Query terms for the lexical half. Word characters only: punctuation in a
# query is noise, but digits and underscores appear in real identifiers.
TERM = re.compile(r"\w+", re.UNICODE)


@dataclass
class SearchHit:
    chunk: object
    score: float
    lexical_rank: int = None
    vector_rank: int = None
    lexical_score: float = 0.0
    vector_score: float = 0.0
    matched_by: tuple = ()

    @property
    def citation(self) -> dict:
        """The section 12.3 shape, minus what does not exist yet."""
        chunk = self.chunk
        return {
            "document_id": chunk.document_version.document_id,
            "document_title": chunk.document_version.document.title,
            "page_number": chunk.page_number,
            "chunk_id": chunk.id,
            "character_start": chunk.character_start,
            "character_end": chunk.character_end,
            "text": chunk.text,
        }


@dataclass
class SearchResponse:
    hits: list = field(default_factory=list)
    query: str = ""
    lexical_candidates: int = 0
    vector_candidates: int = 0
    authorized_documents: int = 0
    semantic: bool = False
    embedding_model: str = ""
    notes: list = field(default_factory=list)


def authorized_chunks(user):
    """Chunks the caller is allowed to see, before any ranking happens.

    Built from the same `authorization.visible_documents` the list endpoint
    uses, so search can never surface a document the API would refuse to
    show directly.

    Restricted to the CURRENT version and the CURRENT extraction pass of
    that version. Both filters are load-bearing, and both were missing:

    *Superseded versions.* Chunks hang off the version that produced them
    and are deliberately never deleted, so a document's whole history stayed
    retrievable. Measured on a two-version regulation, the answerer replied
    "Attendance minimum is 75 percent [1] ... is 60 percent [2]" - quoting
    the WITHDRAWN rule beside the current one, with identical citation
    formatting and no way for a reader to tell which is in force. For a
    regulations platform that is the worst failure available: not a missing
    answer, but a confidently-cited wrong one.

    *Superseded extraction passes.* A reprocess creates a new
    DocumentContent beside the old one on purpose, so two pipeline versions
    can be compared - but each pass writes a full set of chunks too.
    Reprocessing one version took search from 2 hits to 3, of which 2 were
    unique: the same passage retrieved twice, spending the context budget
    on a duplicate and letting one piece of evidence be counted as two.

    History is still in the database and still reachable through the
    version endpoints. It is simply not what a question gets answered from.
    """
    # Archived documents keep their chunks - restoring one must not need a
    # re-index - but they are not what a question is answered from.
    documents = authorization.visible_documents(
        user, Document.objects.filter(archived_at__isnull=True)
    )

    return DocumentChunk.objects.filter(
        document_version__document__in=documents,
        document_version=F("document_version__document__current_version"),
        document_content=F("document_version__current_content"),
    ), documents


def _why_nothing_is_searchable(user, documents) -> str:
    """Say which of three different situations produced no results.

    The note here used to read "no authorized documents match the filters"
    whenever the CHUNK queryset was empty, which conflates a permission
    outcome with an indexing one. Observed: four documents plainly listed
    and processed, search returning nothing, and a note insisting there
    were no authorized documents. The cause was that every chunk belonged
    to an extraction pass that was no longer the live one - a fact the
    message actively pointed away from.

    Distinguishing the cases costs one COUNT on a path that has already
    decided to return nothing, and turns a silent, misleading dead end into
    something diagnosable.
    """
    visible = documents.count()
    if not visible:
        return "no authorized documents match the filters"

    # Same documents, without the "current version, current pass" bound.
    existing = DocumentChunk.objects.filter(
        document_version__document__in=documents
    ).count()

    if not existing:
        return (
            f"{visible} authorized document(s), but none of them has been "
            f"indexed - the INDEXING stage produced no chunks"
        )

    return (
        f"{visible} authorized document(s) with {existing} chunk(s), none of "
        f"which belongs to the current version's current extraction pass. "
        f"Re-process the documents, or check that the worker is running the "
        f"same code as the API"
    )


def _build_lexical_query(query: str):
    """OR the query's terms rather than AND-ing them.

    Django's SearchQuery(text) produces a plainto_tsquery, which requires
    EVERY term to be present. On a natural-language query that is close to
    useless: "attendance percentage required for exams" matches nothing,
    because no chunk contains all four stems even though "attendance" is
    exactly what the user meant. OR-ing means partial matches are found, and
    SearchRank still puts the chunks matching more terms on top - which is
    the behaviour a search box is expected to have.

    Both forms are OR-ed together, because each rescues a case the other
    loses. Postgres tokenizes "REG/2024/117" as ONE compound lexeme, so
    splitting it into REG / 2024 / 117 finds nothing - the whole-query form
    is what matches an exact identifier, and identifiers are precisely what
    section 12.2 says lexical search is for. Conversely the whole-query form
    AND-s a sentence into oblivion, which the per-term form rescues.
    """
    combined = SearchQuery(query, config="english")

    for term in TERM.findall(query):
        if len(term) > 1:
            combined |= SearchQuery(term, config="english")

    return combined


def _lexical(queryset, query: str, limit: int):
    search_query = _build_lexical_query(query)
    if search_query is None:
        return []

    return list(
        queryset.filter(search_vector=search_query)
        .annotate(rank=SearchRank(F("search_vector"), search_query))
        .order_by("-rank", "id")
        .select_related("document_version__document")[:limit]
    )


def _vector(queryset, vector, model_name, model_version, limit: int):
    """Nearest chunks by cosine distance, restricted to one model's vectors.

    Mixing models would compare coordinates from different spaces, which
    produces numbers that look like scores and mean nothing.
    """
    return list(
        queryset.filter(
            embeddings__model_name=model_name,
            embeddings__model_version=model_version,
        )
        .annotate(distance=CosineDistance("embeddings__embedding", vector))
        .order_by("distance", "id")
        .select_related("document_version__document")[:limit]
    )


def search(
    user,
    query: str,
    *,
    limit: int = 10,
    document_type: str = "",
    department: str = "",
    document_id: int | None = None,
) -> SearchResponse:
    query = (query or "").strip()
    if not query:
        return SearchResponse(query=query, notes=["empty query"])

    candidates = getattr(settings, "SEARCH_CANDIDATES", 50)

    # --- Authorization first, always -------------------------------------
    queryset, documents = authorized_chunks(user)

    if document_type:
        queryset = queryset.filter(
            document_version__document__document_type=document_type
        )
    if department:
        queryset = queryset.filter(document_version__document__department=department)
    if document_id:
        # Scoped questions ("ask about this document"). Applied AFTER the
        # authorization filter like every other narrowing, so naming a
        # document you cannot read returns nothing - not an error that
        # would confirm the id exists.
        queryset = queryset.filter(document_version__document_id=document_id)

    response = SearchResponse(query=query, authorized_documents=documents.count())

    if not queryset.exists():
        response.notes.append(_why_nothing_is_searchable(user, documents))
        return response

    # --- Lexical half -----------------------------------------------------
    lexical = _lexical(queryset, query, candidates)
    response.lexical_candidates = len(lexical)

    # --- Vector half ------------------------------------------------------
    vector_hits = []
    try:
        provider = chunking_service.get_provider()
    except EmbeddingProviderNotConfigured:
        response.notes.append("no embedding provider configured - lexical results only")
        provider = None
    except EmbeddingProviderUnavailable as exc:
        response.notes.append(f"embedding provider unavailable: {exc}")
        provider = None

    if provider is not None:
        response.semantic = provider.semantic
        response.embedding_model = f"{provider.name} {provider.version}"
        if not provider.semantic:
            response.notes.append(
                "embeddings are a lexical hashing baseline, not semantic - "
                "vector results reflect vocabulary overlap, not meaning"
            )

        # Embedding the QUERY is a second, separate failure point from
        # loading the provider, and it was unguarded. That cost nothing
        # while the only embedder was the hashing baseline, which is pure
        # arithmetic and cannot fail. It is not exotic for a real one: a
        # local model downloads its weights on first use, so a cold cache,
        # a read-only cache directory or a half-installed torch turns the
        # FIRST SEARCH AFTER A DEPLOY into a 500 - while the lexical half
        # sitting right above this line is perfectly healthy and would have
        # answered the question.
        #
        # Degrading here is the same judgement `chunk_content` already
        # makes when it indexes chunks without vectors: half a search is
        # more useful than none. What is NOT acceptable is substituting a
        # vector from somewhere else - a vector from another model is not
        # comparable, and fusing one in would silently corrupt the ranking
        # rather than narrow it.
        try:
            batch = provider.embed([query])
        except Exception as exc:
            response.notes.append(
                f"query could not be embedded ({type(exc).__name__}) - "
                f"lexical results only"
            )
            response.semantic = False
            logger.warning(
                "search.query_embedding_failed",
                extra={
                    "provider": provider.name,
                    "version": provider.version,
                    "error": type(exc).__name__,
                },
            )
        else:
            vector_hits = _vector(
                queryset, batch.vectors[0], provider.name, provider.version, candidates
            )
            response.vector_candidates = len(vector_hits)

    # --- Fusion -----------------------------------------------------------
    response.hits = _fuse(lexical, vector_hits, limit)
    return response


def _fuse(lexical, vector_hits, limit: int) -> list:
    """Reciprocal-rank fusion.

    Ranks rather than raw scores, because a BM25-ish rank and a cosine
    distance are not on a comparable scale and normalising them against each
    other would invent a relationship neither one claims.
    """
    scores = {}
    chunks = {}

    for position, chunk in enumerate(lexical, start=1):
        chunks[chunk.id] = chunk
        entry = scores.setdefault(chunk.id, {"score": 0.0, "matched": []})
        entry["score"] += 1.0 / (RRF_K + position)
        entry["lexical_rank"] = position
        entry["lexical_score"] = float(getattr(chunk, "rank", 0.0) or 0.0)
        entry["matched"].append("lexical")

    for position, chunk in enumerate(vector_hits, start=1):
        chunks[chunk.id] = chunk
        entry = scores.setdefault(chunk.id, {"score": 0.0, "matched": []})
        entry["score"] += 1.0 / (RRF_K + position)
        entry["vector_rank"] = position
        # Cosine distance is 0 (identical) to 2 (opposite); report a
        # similarity so a larger number consistently means "closer".
        distance = float(getattr(chunk, "distance", 0.0) or 0.0)
        entry["vector_score"] = 1.0 - distance
        entry["matched"].append("vector")

    ordered = sorted(scores.items(), key=lambda item: (-item[1]["score"], item[0]))

    return [
        SearchHit(
            chunk=chunks[chunk_id],
            score=round(entry["score"], 6),
            lexical_rank=entry.get("lexical_rank"),
            vector_rank=entry.get("vector_rank"),
            lexical_score=round(entry.get("lexical_score", 0.0), 6),
            vector_score=round(entry.get("vector_score", 0.0), 6),
            matched_by=tuple(entry["matched"]),
        )
        for chunk_id, entry in ordered[:limit]
    ]
