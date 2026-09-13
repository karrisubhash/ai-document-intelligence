from django.db import models


class ExtractionResult(models.Model):
    """One extractor's field set for one DocumentContent.

    A run row rather than fields hung off Document, for the same reason
    ClassificationResult is: the same version can be re-extracted under a
    newer schema or a better extractor, and every one of those passes stays
    on record with the schema and the model that produced it.

    Both identities are versioned because either can change independently -
    the same regex code against a revised schema pack is a different
    experiment from new code against the same pack, and a result that does
    not say which changed cannot be compared to anything.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="extraction_results",
    )
    # The normalized content the extractor actually consumed. Downstream AI
    # stages read DocumentContent, never the original upload.
    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extraction_results",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="extraction_results",
    )

    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    schema_name = models.CharField(max_length=100)
    schema_version = models.CharField(max_length=50)

    # The document type the schema was selected by, recorded rather than
    # re-derived: Document.document_type can be edited after the fact, and a
    # stored result must keep saying which schema it was actually run under.
    document_type = models.CharField(max_length=30)

    # Denormalized counts so "how often does this schema find nothing?" is a
    # query rather than a scan of the child rows.
    field_count = models.PositiveIntegerField(default=0)
    found_count = models.PositiveIntegerField(default=0)

    # Identifies exactly what was fed to the extractor, so a result can be
    # reproduced rather than just believed.
    input_reference = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["model_name", "model_version"]),
            models.Index(fields=["schema_name", "schema_version"]),
        ]

    def __str__(self):
        return (
            f"{self.found_count}/{self.field_count} field(s) against "
            f"{self.schema_name} {self.schema_version}"
        )


class ExtractedField(models.Model):
    """One schema field's outcome in one extraction pass.

    Every field the schema declares gets a row, including the ones that were
    not found. Omitting them would make "the extractor looked and found
    nothing" indistinguishable from "the extractor never looked", and would
    leave recall without a denominator.
    """

    class Status(models.TextChoices):
        FOUND = "FOUND", "Found"
        NOT_FOUND = "NOT_FOUND", "Not found"
        INVALID = "INVALID", "Found but failed validation"
        CONFLICTED = "CONFLICTED", "Conflicting values found"

    result = models.ForeignKey(
        ExtractionResult,
        on_delete=models.CASCADE,
        related_name="extracted_fields",
    )
    field_name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices)

    # Empty for every status except FOUND. Never null: a field that was
    # looked for and not found is a stored answer, not a missing one.
    value = models.TextField(blank=True, default="")
    confidence = models.FloatField(default=0.0)

    # Where the value came from. Null - not zero - when there is no span to
    # point at, because character offset 0 is a real position. Captured now
    # rather than backfilled later: the offsets are free at match time and
    # expensive to reconstruct afterwards, and they are what the citation
    # model in section 12.3 will need.
    page_number = models.PositiveIntegerField(null=True, blank=True)
    character_start = models.PositiveIntegerField(null=True, blank=True)
    character_end = models.PositiveIntegerField(null=True, blank=True)
    # The exact slice the offsets point at, stored so a citation can be
    # rendered without re-reading PageContent, and so drift between the two
    # is detectable rather than silent.
    matched_text = models.TextField(blank=True, default="")

    # Room for the competing values behind a CONFLICTED verdict, the
    # validation rule an INVALID value failed, and per-field timings.
    metadata = models.JSONField(blank=True, default=dict)

    class Meta:
        ordering = ["field_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["result", "field_name"],
                name="unique_field_name_per_extraction_result",
            )
        ]

    def __str__(self):
        return f"{self.field_name}={self.value!r} ({self.status})"
