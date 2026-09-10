from django.conf import settings
from django.db import models


def document_version_upload_path(instance, filename):
    return f"documents/{instance.document_id}/versions/{instance.version_number}/{filename}"


class DocumentVersion(models.Model):
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()

    # The original uploaded artifact. Never overwritten by later
    # versions or reprocessing runs - each version gets its own path.
    file = models.FileField(upload_to=document_version_upload_path)
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=100, blank=True, default="")
    checksum = models.CharField(max_length=64, blank=True, default="")

    extracted_text = models.TextField(blank=True, default="")

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_document_versions",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version_number"],
                name="unique_document_version_number",
            )
        ]

    def __str__(self):
        return f"{self.document.title} v{self.version_number}"
