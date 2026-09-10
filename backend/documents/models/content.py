from django.db import models


class DocumentContent(models.Model):
    """Normalized extraction output for one DocumentVersion.

    A new row per processing run rather than one mutable row, so a
    reprocess with a better extractor can be compared against what the
    previous pipeline produced instead of overwriting it.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="contents",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contents",
    )

    # Provenance: which pipeline produced this content.
    processing_version = models.CharField(max_length=50)
    page_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Content for {self.document_version} ({self.page_count} pages)"


class PageContent(models.Model):
    class ExtractionMethod(models.TextChoices):
        PDF_TEXT = "PDF_TEXT", "PDF text layer"
        OCR = "OCR", "OCR"
        PLAIN_TEXT = "PLAIN_TEXT", "Plain text"

    content = models.ForeignKey(
        DocumentContent,
        on_delete=models.CASCADE,
        related_name="pages",
    )
    page_number = models.PositiveIntegerField()
    text = models.TextField(blank=True, default="")
    extraction_method = models.CharField(
        max_length=20,
        choices=ExtractionMethod.choices,
    )
    character_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(blank=True, default=dict)

    class Meta:
        ordering = ["page_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["content", "page_number"],
                name="unique_page_number_per_content",
            )
        ]

    def __str__(self):
        return f"Page {self.page_number} ({self.extraction_method})"
