from django.db import models


class DocumentTable(models.Model):
    """One table found in one page of one extraction pass.

    Hangs off DocumentContent for the same reason chunks do: it is derived
    from a particular extraction, so a reprocess under a better extractor
    produces a new set beside the old rather than silently rewriting the
    structure a previous answer was grounded in.

    The cells are stored as JSON, but the row of text they came from is
    stored too, as a span. That pairing is the point: the JSON is what makes
    a table queryable, and the span is what makes it checkable - every cell
    must re-slice out of the page, so a row that is not in the document
    cannot be in this table.
    """

    document_version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.CASCADE,
        related_name="tables",
    )
    document_content = models.ForeignKey(
        "documents.DocumentContent",
        on_delete=models.CASCADE,
        related_name="tables",
    )
    processing_job = models.ForeignKey(
        "documents.ProcessingJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tables",
    )

    # Ordinal across the whole document, in reading order.
    table_index = models.PositiveIntegerField()

    # Provenance, and the half of a §12.3 citation that a table needs. A
    # table never spans pages: page text is the unit it was detected in.
    page_number = models.PositiveIntegerField()
    character_start = models.PositiveIntegerField()
    character_end = models.PositiveIntegerField()

    # rows[0] may or may not be the header - see `header`.
    rows = models.JSONField(default=list)
    row_count = models.PositiveIntegerField(default=0)
    column_count = models.PositiveIntegerField(default=0)

    # Empty when the first row could not be told apart from the data.
    # Recorded as a separate field rather than a boolean flag so that
    # "no header" and "header is these three labels" are different states,
    # neither of which is a guess presented as a fact.
    header = models.JSONField(blank=True, default=list)

    # Which strategy produced this, so two detectors can be compared rather
    # than one silently replacing the other's output.
    detector = models.CharField(max_length=50)
    detector_version = models.CharField(max_length=50)

    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["table_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_content", "table_index"],
                name="unique_table_index_per_content",
            )
        ]
        indexes = [
            models.Index(fields=["document_version", "page_number"]),
        ]

    def __str__(self):
        return (
            f"table {self.table_index} on page {self.page_number} "
            f"({self.row_count}x{self.column_count})"
        )

    def as_text(self) -> str:
        """A pipe-delimited rendering, for a prompt or a preview.

        Not what gets indexed or quoted: retrieval works on the verbatim
        page region, so a citation points at something a reader can find
        on the page rather than at this reconstruction.
        """
        return "\n".join(" | ".join(row) for row in self.rows)
