from django.db import models


class ProcessingEvent(models.Model):
    class Status(models.TextChoices):
        STARTED = "STARTED", "Started"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        SKIPPED = "SKIPPED", "Skipped"
        FAILED = "FAILED", "Failed"

    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.CASCADE,
        related_name="events",
    )
    stage = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=Status.choices)
    message = models.TextField(blank=True, default="")
    metadata = models.JSONField(blank=True, default=dict)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.stage} - {self.status}"
