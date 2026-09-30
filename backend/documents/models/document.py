from django.conf import settings
from django.db import models


class Document(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        # Distinct from FAILED on purpose (spec 10.1). A quarantined
        # document is not a processing error to retry - it is a file
        # the platform has decided not to touch, and the distinction
        # is what an operator needs to see.
        QUARANTINED = "QUARANTINED", "Quarantined"

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

    # Archival - the reversible half of the deletion policy. An archived
    # document keeps every version, extraction pass and audit row, and the
    # people who could read it still can; what it loses is reach. It is out
    # of search, out of RAG and out of the default listing, and it accepts
    # no new version and no reprocessing until someone restores it.
    #
    # A timestamp rather than a status value on purpose: archiving is
    # orthogonal to processing. A COMPLETED document that is archived is
    # still completed, and folding the two into one field would make
    # restoring it a guess about which status it used to have.
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # `department` sits on the authorization boundary itself -
            # `authorization.visible_documents` ORs it with `owner_id` on
            # EVERY listing request from a non-admin user, not only when a
            # caller explicitly filters by it. Unindexed, that OR forced a
            # parallel sequential scan: measured at ~26k rows, 44-76ms per
            # listing (documents/tests_discovery.py has the fixture used to
            # get all the numbers in this comment; the discovery section of
            # the README has the full before/after table).
            #
            # `-created_at` rides along for a narrower, real reason, not a
            # speculative one: a caller who filters by department with NO
            # owner clause at all - an explicit `?department=` filter from a
            # SYSTEM_ADMIN, whose authorization scope carries no OR to begin
            # with - lets Postgres walk this index directly in the order
            # `ORDER BY -created_at` already wants: an `Index Scan using
            # document_dept_created_idx` with a cheap incremental sort that
            # stops once LIMIT 50 is satisfied, rather than a Bitmap Heap
            # Scan that has to materialize and sort the whole matching set
            # first. Measured at 40k rows / ~1,000 matching a department:
            # 1.76ms this way. (The single-column index was not measured
            # against this exact query shape - filter with no OR - only
            # against the authorization-scoped one below, where the two
            # are close either way.)
            #
            # WHAT THIS DOES NOT DO: for the dominant case - the
            # authorization OR still present, as it is on every non-admin
            # request - the extra column bought nothing measurable over a
            # plain `department` index in these tests. At low department
            # cardinality (6 departments, ~33% selectivity each) it was
            # measurably WORSE (11.5ms against 6.4ms), because the wider
            # composite index costs more per bitmap entry and that pushed
            # Postgres to prefer a sequential scan where the leaner
            # single-column index still won the planner over to a bitmap
            # scan. At realistic cardinality (40 departments, ~2.5% each,
            # closer to an actual campus) the two were close either way
            # (1.8-2.8ms). Kept anyway: the admin-dashboard case above is a
            # clear, reproducible win with no realistic downside, and both
            # numbers are comfortably inside acceptable request latency at
            # any scale measured so far.
            models.Index(
                fields=["department", "-created_at"], name="document_dept_created_idx"
            ),
            # `status` alone, for the query the index above does not help at
            # all: a SYSTEM_ADMIN's listing carries no department/owner
            # restriction (`visible_documents` returns the queryset
            # untouched), so "show me every FAILED document" - an
            # operational query this filter exists for - has no department
            # in its WHERE clause for a department-leading index to use.
            # Measured: a plain `Bitmap Index Scan on document_status_idx`
            # is what the planner chooses for exactly that query once this
            # index exists; see documents/tests_discovery.py for the
            # end-to-end filter test and the README for the full timing
            # table.
            models.Index(fields=["status"], name="document_status_idx"),
        ]

    def __str__(self):
        return self.title

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None
