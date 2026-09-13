from django.db import models


class DocumentFigure(models.Model):
    """One image embedded in one page of one extraction pass.

    Stores a fact rather than an interpretation: this page embeds an image
    of these dimensions. There is no `kind` field saying CHART or DIAGRAM,
    because nothing in this pipeline can tell those apart, and a column
    that named them would be read as a measurement by everything
    downstream.

    `description` is empty until a describer is configured. That empty
    string is the useful part: it is what lets an answer say "there is an
    image on page 4 that I cannot read" rather than either inventing a
    caption or staying silent about content the document plainly has.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="figures",
    )
    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.CASCADE,
        related_name="figures",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="figures",
    )

    page_number = models.PositiveIntegerField()
    figure_index = models.PositiveIntegerField()

    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)

    description = models.TextField(blank=True, default="")
    # Which describer produced the description, so a caption can never be
    # mistaken for something the document itself said.
    description_source = models.CharField(max_length=100, blank=True, default="")

    detector = models.CharField(max_length=50)
    detector_version = models.CharField(max_length=50)

    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["page_number", "figure_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_content", "page_number", "figure_index"],
                name="unique_figure_per_page_and_content",
            )
        ]
        indexes = [
            models.Index(fields=["document_version", "page_number"]),
        ]

    def __str__(self):
        return (
            f"figure {self.figure_index} on page {self.page_number} "
            f"({self.width}x{self.height})"
        )
