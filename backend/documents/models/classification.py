from django.db import models


class ClassificationResult(models.Model):
    """One classifier's verdict on one DocumentVersion.

    Deliberately not a field on Document: the same document can be
    classified repeatedly - a new version, or the same version re-run
    under a newer model - and every one of those verdicts stays on
    record with the model that produced it.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="classification_results",
    )
    # The normalized content the classifier actually consumed. Downstream
    # AI stages read DocumentContent, never the original upload.
    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="classification_results",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="classification_results",
    )

    model_name = models.CharField(max_length=100)
    model_version = models.CharField(max_length=50)
    predicted_label = models.CharField(max_length=100)
    confidence = models.FloatField()

    # Identifies exactly what was fed to the model, so a prediction can be
    # reproduced rather than just believed.
    input_reference = models.CharField(max_length=255, blank=True, default="")
    # Room for the full score distribution, timings, aggregation details.
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["model_name", "model_version"]),
        ]

    def __str__(self):
        return (
            f"{self.predicted_label} ({self.confidence:.2f}) "
            f"by {self.model_name} {self.model_version}"
        )
