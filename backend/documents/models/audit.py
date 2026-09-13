from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    class Action(models.TextChoices):
        DOCUMENT_VIEW = "DOCUMENT_VIEW", "Document view"
        DOCUMENT_CREATE = "DOCUMENT_CREATE", "Document create"
        DOCUMENT_UPDATE = "DOCUMENT_UPDATE", "Document update"
        DOCUMENT_DELETE = "DOCUMENT_DELETE", "Document delete"
        DOCUMENT_DOWNLOAD = "DOCUMENT_DOWNLOAD", "Document download"
        DOCUMENT_REPROCESS = "DOCUMENT_REPROCESS", "Document reprocess"
        DOCUMENT_VERSION_UPLOAD = "DOCUMENT_VERSION_UPLOAD", "Document version upload"
        SEARCH = "SEARCH", "Search"
        CHAT = "CHAT", "Chat"

    class Result(models.TextChoices):
        ALLOWED = "ALLOWED", "Allowed"
        DENIED = "DENIED", "Denied"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    result = models.CharField(max_length=10, choices=Result.choices)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} {self.action} {self.result}"
