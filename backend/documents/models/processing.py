from django.db import models


class ProcessingJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class Stage(models.TextChoices):
        INGESTION = "INGESTION", "Ingestion"
        VALIDATION = "VALIDATION", "Validation"
        SCANNING = "SCANNING", "Malware scanning"
        TEXT_EXTRACTION = "TEXT_EXTRACTION", "Text extraction"
        OCR = "OCR", "OCR"
        CLASSIFICATION = "CLASSIFICATION", "Classification"
        LAYOUT_ANALYSIS = "LAYOUT_ANALYSIS", "Layout analysis"
        EXTRACTION = "EXTRACTION", "Extraction"
        INDEXING = "INDEXING", "Indexing"

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="processing_jobs",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    current_stage = models.CharField(
        max_length=30,
        choices=Stage.choices,
        blank=True,
        default="",
    )
    attempt = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=100, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Job #{self.id} for {self.document_version}"
