from django.db import models
from pgvector.django import HnswIndex, VectorField

from .chunk import EMBEDDING_DIMENSIONS


class DocumentInsight(models.Model):
    """What a reader should know about one extraction pass, before reading it.

    The brief of a document: an extractive summary, key points, key phrases,
    reading statistics and questions the document can answer. Computed after
    the pass is published, on the index queue, from the stored page text and
    the chunks already indexed - never from the original upload, and never by
    a language model. Every sentence in `summary` and `key_points` is a span
    of `PageContent.text` (page_number, start, end in Unicode code points),
    verified to re-slice exactly before it is stored, so each one is a
    citation rather than a paraphrase.

    One row per extraction pass. A pass is immutable, so regenerating its
    brief - a newer generator, or the type changed and the questions with
    it - replaces the row in place rather than appending a second one, which
    is also what makes the enrichment task safe to redeliver.

    The `ai_*` columns are reserved for an on-demand, user-requested LLM
    brief. Nothing writes them yet: the ingest path never calls a model, and
    a NULL there means exactly "not generated".
    """

    class Status(models.TextChoices):
        READY = "READY", "Ready"
        # The generator raised. Recorded rather than left absent so the API
        # can say "failed" instead of "still being prepared" forever.
        FAILED = "FAILED", "Failed"

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="insights",
    )
    document_content = models.OneToOneField(
        "documents.DocumentContent",
        on_delete=models.CASCADE,
        related_name="insight",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="insights",
    )

    status = models.CharField(max_length=10, choices=Status.choices)

    # Which code produced this, so a brief can be traced to the rules that
    # wrote it and regenerated when they change.
    generator_name = models.CharField(max_length=100)
    generator_version = models.CharField(max_length=50)

    # [{text, page_number, start, end, score}] in reading order.
    summary = models.JSONField(blank=True, default=list)
    # [{text, page_number, start, end, kind}].
    key_points = models.JSONField(blank=True, default=list)
    # [{text, score}], best first.
    key_phrases = models.JSONField(blank=True, default=list)
    # {pages, words, reading_minutes, ocr_pages, language}.
    stats = models.JSONField(blank=True, default=dict)
    # [str] - each one checked against this document's own chunks.
    suggested_questions = models.JSONField(blank=True, default=list)
    # Timings, candidate counts, which signals were available, the
    # self-check outcome - everything needed to explain a brief later.
    metadata = models.JSONField(blank=True, default=dict)
    # Internal only (FAILED): never returned by the API.
    error_message = models.TextField(blank=True, default="")

    # Reserved for the on-demand LLM brief. `ai_generated_at` NULL means
    # "not generated", and the API then returns `"ai": null` whatever the
    # other three hold. Text columns are empty rather than NULL, the
    # project's rule for string fields.
    ai_summary = models.TextField(blank=True, default="")
    ai_questions = models.JSONField(null=True, blank=True)
    ai_model = models.CharField(max_length=100, blank=True, default="")
    ai_generated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Insight for content {self.document_content_id} ({self.status})"


class DocumentEntity(models.Model):
    """One typed fact found in an extraction pass: a date, an amount, a contact.

    Rows rather than a JSON list on the insight because the interesting
    questions about entities cross documents - "which deadlines fall in the
    next three months, in documents I can read?" - and a date index answers
    that where a scan of every brief would not.

    Deduplicated per pass by normalised value: `occurrences` counts them,
    and the span is the first one in reading order, verified to re-slice
    exactly from the stored page text like every other span here.
    """

    class EntityType(models.TextChoices):
        DATE = "DATE", "Date"
        # A date the document attaches an obligation to: "last date",
        # "on or before", "not later than".
        DEADLINE = "DEADLINE", "Deadline"
        MONEY = "MONEY", "Amount"
        PERCENT = "PERCENT", "Percentage"
        DURATION = "DURATION", "Duration"
        EMAIL = "EMAIL", "Email"
        PHONE = "PHONE", "Phone"
        URL = "URL", "Link"
        REFERENCE = "REFERENCE", "Reference number"
        COURSE_CODE = "COURSE_CODE", "Course code"
        ORG = "ORG", "Organisation"

    insight = models.ForeignKey(
        DocumentInsight,
        on_delete=models.CASCADE,
        related_name="entities",
    )
    # Denormalised from the insight so cross-document queries can join
    # straight to the version - and through it, to authorization.
    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="+",
    )

    entity_type = models.CharField(max_length=20, choices=EntityType.choices)
    # The exact slice of the page, as the document wrote it.
    text = models.TextField()
    # A canonical form (ISO date, "INR 4500", E.164 phone...), or empty when
    # the text could not be normalised - never a guess at one. The API
    # returns empty as null.
    normalized = models.CharField(max_length=255, blank=True, default="")
    # The normalised date, for DATE and DEADLINE, so ranges are a query.
    value_date = models.DateField(null=True, blank=True)
    # What a deadline is for, in the document's words ("Last date for
    # payment of fees"). Empty when there is nothing to say.
    label = models.CharField(max_length=200, blank=True, default="")

    page_number = models.PositiveIntegerField()
    character_start = models.PositiveIntegerField()
    character_end = models.PositiveIntegerField()
    occurrences = models.PositiveIntegerField(default=1)
    # Reading order of the first occurrence, for a stable listing.
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position"]
        indexes = [
            models.Index(fields=["value_date"], name="entity_value_date_idx"),
            models.Index(
                fields=["entity_type", "normalized"], name="entity_type_norm_idx"
            ),
        ]

    def __str__(self):
        return f"{self.entity_type} {self.text!r}"


class DocumentCentroid(models.Model):
    """The mean of one extraction pass's chunk embeddings, by one model.

    What "related documents" compares: one vector per pass, so finding the
    nearest documents is one indexed nearest-neighbour query instead of a
    comparison of every chunk with every other. Keyed by model exactly as
    chunk embeddings are - a centroid is only comparable to another made
    from the same model's vectors - and it records whether those vectors
    were semantic, because a lexical baseline's "similar" means shared
    vocabulary, not shared meaning.
    """

    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.CASCADE,
        related_name="centroids",
    )
    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="+",
    )

    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    dimensions = models.PositiveIntegerField()
    semantic = models.BooleanField(default=False)
    # How many chunk vectors were averaged.
    chunk_count = models.PositiveIntegerField(default=0)

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document_content", "model_name", "model_version"],
                name="unique_centroid_per_content_and_model",
            )
        ]
        indexes = [
            HnswIndex(
                name="document_centroid_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self):
        return f"centroid of content {self.document_content_id} ({self.model_name})"


def thumbnail_upload_path(instance, filename):
    version = instance.document_version
    return (
        f"documents/{version.document_id}/versions/{version.version_number}/"
        f"thumbnails/{filename}"
    )


class DocumentThumbnail(models.Model):
    """A small picture of a version's first page, for cards and grids.

    Per version, not per extraction pass: it is a picture of the uploaded
    file, which a reprocess does not change. Served only through the
    authenticated thumbnail endpoint, which refuses quarantined versions -
    never as a public media URL.
    """

    document_version = models.OneToOneField(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="thumbnail",
    )
    file = models.FileField(upload_to=thumbnail_upload_path, max_length=255)
    mime_type = models.CharField(max_length=50)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    source_page = models.PositiveIntegerField(default=1)
    generator = models.CharField(max_length=100)
    generator_version = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"thumbnail of version {self.document_version_id}"
