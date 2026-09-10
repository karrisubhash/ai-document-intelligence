from django.conf import settings
from django.db import models


class Document(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    class DocumentType(models.TextChoices):
        GENERAL = "GENERAL", "General"
        REGULATION = "REGULATION", "Regulation"
        POLICY = "POLICY", "Policy"
        SYLLABUS = "SYLLABUS", "Syllabus"
        CIRCULAR = "CIRCULAR", "Circular"
        FORM = "FORM", "Form"
        OTHER = "OTHER", "Other"

    title = models.CharField(max_length=255)
    document_type = models.CharField(
        max_length=30,
        choices=DocumentType.choices,
        default=DocumentType.GENERAL,
    )
    department = models.CharField(max_length=255, blank=True, default="")

    # Ownership is left nullable until the authentication/authorization
    # milestone exists. We don't fake ownership with anonymous requests.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_documents",
    )

    current_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UPLOADED,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
