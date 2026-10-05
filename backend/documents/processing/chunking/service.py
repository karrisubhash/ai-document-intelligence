import functools
from contextlib import contextmanager

from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import connection, transaction
from django.utils.module_loading import import_string

from documents.models import DocumentChunk, DocumentChunkEmbedding, DocumentContent
from documents.models.chunk import EMBEDDING_DIMENSIONS

from . import splitter
from .exceptions import (
    ChunkingError,
    EmbeddingProviderNotConfigured,
    EmbeddingProviderUnavailable,
)

# Bumped whenever chunking changes in a way that alters its output, so a
# stored chunk can be traced back to the strategy that produced it.
CHUNKING_VERSION = "chunking-v1"
CHUNKING_STRATEGY = "page-bounded-overlap"

# Embedding in batches keeps a remote provider's request count sane and a
# local one's memory bounded.
EMBED_BATCH_SIZE = 64


@functools.cache
def _load_provider(path: str) -> object:
    """Instantiate once per dotted path.

    Keyed on the path alone deliberately: unlike the classifier and field
    extractor, a provider's identity is its (name, version, dimensions),
    which are attributes of the class rather than of a settings-supplied
    artifact. There is no second setting whose change could leave a stale
    instance behind.
    """
    return import_string(path)()


def reset_provider_cache() -> None:
    _load_provider.cache_clear()


def get_provider():
    path = getattr(settings, "DOCUMENT_EMBEDDING_PROVIDER", "")

    if not path:
        raise EmbeddingProviderNotConfigured(
            "No DOCUMENT_EMBEDDING_PROVIDER configured. Chunks are stored "
            "and lexically searchable, but no vectors are produced."
        )

    try:
        provider = _load_provider(path)
    except ImportError as exc:
        raise EmbeddingProviderUnavailable(
            f"Could not load embedding provider {path!r}: {exc}"
        ) from exc

    if provider.dimensions != EMBEDDING_DIMENSIONS:
        # Storing a vector of the wrong width would fail at the database, or
        # worse, succeed against a differently-shaped column and make every
        # distance meaningless.
        raise EmbeddingProviderUnavailable(
            f"Provider {provider.name} emits {provider.dimensions}-dimensional "
            f"vectors but the column is {EMBEDDING_DIMENSIONS}. Changing the "
            f"embedding model requires a migration and a full re-index."
        )

    return provider


def load_pages(content: DocumentContent) -> list:
    return list(
        content.pages.order_by("page_number").values_list("page_number", "text")
    )


def load_tables(content: DocumentContent) -> dict:
    """Tables per page, for the chunker to keep intact.

    Read back from the database rather than passed down from the stage:
    chunking also runs from the re-index task, which starts from stored
    content and has no pipeline context to inherit. Reading one source
    means a re-index chunks a document exactly as ingestion did.
    """
    tables = {}
    for table in content.tables.order_by("table_index"):
        tables.setdefault(table.page_number, []).append(table)
    return tables


# Namespace for this module's advisory locks, so they cannot collide with
# another use of pg_advisory_lock on the same database.
LOCK_NAMESPACE = 7301


@contextmanager
def content_lock(content_id: int):
    """Serialize chunking of ONE extraction pass across every process.

    The pipeline's INDEXING stage and a `reindex` task can chunk the same
    DocumentContent at the same time (`reindex --stale-only` picks exactly
    the passes the pipeline has just written and not yet indexed). Each
    transaction deleted the chunks it could see - none of the other's
    uncommitted rows - and inserted its own, so both sets survived and every
    passage was retrieved twice; or one side deleted chunks the other was
    about to embed, and that job failed. A row lock would not be enough:
    embedding happens after the chunk transaction commits. A session-level
    advisory lock spans both, needs no open transaction, and is released by
    Postgres itself if the worker dies.
    """
    key = int(content_id) & 0x7FFFFFFF
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s, %s)", [LOCK_NAMESPACE, key])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, key])


def chunk_content(content_id: int, *, processing_job=None) -> dict:
    """Chunk, embed and index one DocumentContent - one process at a time."""
    with content_lock(content_id):
        return _chunk_content(content_id, processing_job=processing_job)


def _chunk_content(content_id: int, *, processing_job=None) -> dict:
    """Chunk one DocumentContent, embed the chunks, and index them.

    Returns a summary. Embedding is optional: when no provider is
    configured the chunks are still written and still lexically searchable,
    because half a search is more useful than none and the vectors can be
    backfilled later.
    """
    content = DocumentContent.objects.select_related("document_version").get(
        id=content_id
    )
    pages = load_pages(content)

    chunks = splitter.split_pages(
        pages,
        tables_by_page=load_tables(content),
        chunk_size=getattr(settings, "CHUNK_SIZE", splitter.DEFAULT_CHUNK_SIZE),
        overlap=getattr(settings, "CHUNK_OVERLAP", splitter.DEFAULT_OVERLAP),
    )

    # The same invariant field extraction enforces: a stored offset that
    # does not re-slice to its own text is not a citation.
    try:
        splitter.verify(chunks, pages)
    except ValueError as exc:
        raise ChunkingError(str(exc)) from exc

    if not chunks:
        return {
            "chunks": 0,
            "embedded": 0,
            "embedding_model": "",
            "semantic": False,
            "skipped_reason": "no text to chunk",
        }

    with transaction.atomic():
        # Replacing this content's own chunks, for the same reason the
        # pipeline replaces its own DocumentContent: a retry must not
        # append a second copy of every chunk.
        DocumentChunk.objects.filter(document_content=content).delete()

        rows = DocumentChunk.objects.bulk_create(
            [
                DocumentChunk(
                    document_version=content.document_version,
                    document_content=content,
                    processing_job=processing_job,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    character_start=chunk.character_start,
                    character_end=chunk.character_end,
                    text=chunk.text,
                    character_count=len(chunk.text),
                    chunking_strategy=CHUNKING_STRATEGY,
                    chunking_version=CHUNKING_VERSION,
                    metadata=chunk.metadata,
                )
                for chunk in chunks
            ]
        )

        # The lexical half of hybrid retrieval. Computed in the database so
        # the stored vector matches what the query side will produce.
        DocumentChunk.objects.filter(document_content=content).update(
            search_vector=SearchVector("text", config="english")
        )

    summary = {
        "chunks": len(rows),
        "embedded": 0,
        "embedding_model": "",
        "semantic": False,
        "strategy": CHUNKING_STRATEGY,
        "chunking_version": CHUNKING_VERSION,
        "pages": len(pages),
    }

    try:
        provider = get_provider()
    except EmbeddingProviderNotConfigured as exc:
        summary["skipped_reason"] = str(exc)
        return summary

    summary.update(embed_chunks(rows, provider))
    return summary


def embed_chunks(chunks, provider) -> dict:
    """Embed stored chunks and write the vectors."""
    embedded = 0

    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        try:
            result = provider.embed([chunk.text for chunk in batch])
        except Exception as exc:
            # A provider failure is worth retrying - a remote one may be
            # briefly unavailable, and the chunks are already safely stored.
            raise EmbeddingProviderUnavailable(
                f"Embedding provider {provider.name} failed: {exc}"
            ) from exc

        if len(result.vectors) != len(batch):
            raise ChunkingError(
                f"Provider {provider.name} returned {len(result.vectors)} "
                f"vectors for {len(batch)} texts."
            )

        DocumentChunkEmbedding.objects.bulk_create(
            [
                DocumentChunkEmbedding(
                    chunk=chunk,
                    model_name=result.model_name,
                    model_version=result.model_version,
                    dimensions=result.dimensions,
                    semantic=result.semantic,
                    embedding=vector,
                    metadata=result.metadata,
                )
                # strict=True: a provider that returned fewer vectors
                # than chunks would otherwise embed a prefix and leave
                # the rest unsearchable with nothing logged.
                for chunk, vector in zip(batch, result.vectors, strict=True)
            ],
            update_conflicts=True,
            update_fields=["embedding", "dimensions", "semantic", "metadata"],
            unique_fields=["chunk", "model_name", "model_version"],
        )
        embedded += len(batch)

    return {
        "embedded": embedded,
        "embedding_model": f"{provider.name} {provider.version}",
        "semantic": provider.semantic,
    }
