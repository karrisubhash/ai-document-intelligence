from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField

# Fixed at the column, because a pgvector index is built for one width.
#
# 384 is the width of the small sentence-transformer family - bge-small,
# e5-small, all-MiniLM - which is what this platform embeds with by
# default. That family is the right default for a corpus this size on CPU:
# bge-small is 33M parameters and embeds a twelve-thousand-document corpus
# in minutes, where a 1024-wide model takes hours and needs a GPU to be
# reasonable.
#
# A model of a different width needs a migration AND a full re-index, which
# is the "controlled re-indexing strategy" section 11.4 asks for rather
# than something to slip in quietly. Amazon Titan Text Embeddings V2, for
# instance, emits 256, 512 or 1024 and none of them is 384 - so switching
# to it is a schema change, not a settings change. See
# documents/processing/embedding/bedrock.py.
EMBEDDING_DIMENSIONS = 384


class DocumentChunk(models.Model):
    """One retrievable unit of a document, with where it came from.

    Chunks hang off DocumentContent rather than Document: they are derived
    from one extraction pass, so a reprocess under a better extractor
    produces a new set beside the old rather than silently mutating what a
    previous answer was grounded in.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chunks",
    )

    # Ordinal across the whole document, so chunks can be put back in
    # reading order without sorting on offsets across pages.
    chunk_index = models.PositiveIntegerField()

    # Provenance. A chunk never spans pages, so exactly one page number is
    # always correct - and these three fields are most of a section 12.3
    # citation already.
    page_number = models.PositiveIntegerField()
    character_start = models.PositiveIntegerField()
    character_end = models.PositiveIntegerField()

    text = models.TextField()
    character_count = models.PositiveIntegerField(default=0)

    # Which strategy produced this, so two chunkings can be compared rather
    # than one silently replacing the other's results.
    chunking_strategy = models.CharField(max_length=50)
    chunking_version = models.CharField(max_length=50)

    # Maintained by the indexing step for the lexical half of hybrid search.
    search_vector = SearchVectorField(null=True, blank=True)

    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_content", "chunk_index"],
                name="unique_chunk_index_per_content",
            )
        ]
        indexes = [
            # The lexical half of hybrid retrieval.
            GinIndex(fields=["search_vector"], name="chunk_search_vector_gin"),
            # Retrieval always filters by document first (the permission
            # filter runs before similarity), so this is the hot path.
            models.Index(fields=["document_version", "page_number"]),
        ]

    def __str__(self):
        return f"chunk {self.chunk_index} (page {self.page_number})"


class DocumentChunkEmbedding(models.Model):
    """One embedding of one chunk, by one model.

    A separate table rather than a column on DocumentChunk so that a new
    embedding model can be indexed alongside the old one and compared before
    anything is thrown away - re-indexing under section 11.4 should be a
    migration you can evaluate, not a cutover you hope worked.
    """

    chunk = models.ForeignKey(
        DocumentChunk,
        on_delete=models.CASCADE,
        related_name="embeddings",
    )

    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    dimensions = models.PositiveIntegerField()

    # Whether this vector encodes meaning or merely vocabulary overlap. The
    # bundled baseline is lexical, and a result that did not say so would
    # overstate what the search did.
    semantic = models.BooleanField(default=False)

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)

    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "model_name", "model_version"],
                name="unique_embedding_per_chunk_and_model",
            )
        ]
        indexes = [
            models.Index(fields=["model_name", "model_version"]),
            HnswIndex(
                name="chunk_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self):
        return f"{self.model_name} {self.model_version} ({self.dimensions}d)"
