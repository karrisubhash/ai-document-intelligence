from django.conf import settings
from django.db import models


class EvaluationDataset(models.Model):
    """A fixed benchmark of questions with known answers.

    Versioned, because a number is only comparable to another number
    measured on the same set. Changing the questions is a new version, not
    an edit - otherwise last month's score and this month's are describing
    different exams and the comparison is meaningless.
    """

    name = models.CharField(max_length=100)
    version = models.CharField(max_length=50)
    description = models.TextField(blank=True, default="")

    # Scoped like documents are, so a benchmark built from one department's
    # corpus is not silently run against another's.
    department = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evaluation_datasets",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "version"], name="unique_dataset_name_and_version"
            )
        ]

    def __str__(self):
        return f"{self.name} {self.version}"


class EvaluationCase(models.Model):
    """One question, and what a correct system would do with it.

    The shape follows spec §19.1, with one addition: `expect_abstention`.
    A benchmark made only of answerable questions cannot tell a
    well-calibrated system from one that never abstains, so the negative
    cases have to be first-class rather than an afterthought.
    """

    dataset = models.ForeignKey(
        EvaluationDataset, on_delete=models.CASCADE, related_name="cases"
    )
    question = models.TextField()
    expected_answer = models.TextField(blank=True, default="")

    # Document ids and page numbers that genuinely answer the question.
    relevant_documents = models.JSONField(blank=True, default=list)
    relevant_pages = models.JSONField(blank=True, default=list)

    # True for questions the corpus deliberately cannot answer. These are
    # what make abstention measurable rather than assumed.
    expect_abstention = models.BooleanField(default=False)

    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.question[:60]


class EvaluationRun(models.Model):
    """One execution of a dataset against the live pipeline.

    Stores the configuration it ran under, not just the score. Section 19.2
    requires results to be kept with model, prompt and pipeline version,
    because a metric without the configuration that produced it cannot be
    compared to anything - it is a number, not a measurement.
    """

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    dataset = models.ForeignKey(
        EvaluationDataset, on_delete=models.CASCADE, related_name="runs"
    )
    label = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING
    )

    # The whole pipeline configuration, captured at run time.
    configuration = models.JSONField(blank=True, default=dict)
    # Aggregated retrieval / generation / system metrics.
    metrics = models.JSONField(blank=True, default=dict)

    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="evaluation_runs",
    )

    cases_total = models.PositiveIntegerField(default=0)
    cases_scored = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["dataset", "-started_at"])]

    def __str__(self):
        return f"{self.dataset} run #{self.id} ({self.status})"


class EvaluationCaseResult(models.Model):
    """What the system actually did with one case, and how it scored.

    The answer and its citations are stored rather than recomputed: a
    metric you cannot trace back to the output that produced it is not
    auditable, and re-running retrieval later would consult a different
    index.
    """

    run = models.ForeignKey(
        EvaluationRun, on_delete=models.CASCADE, related_name="results"
    )
    case = models.ForeignKey(
        EvaluationCase, on_delete=models.CASCADE, related_name="results"
    )

    answer = models.TextField(blank=True, default="")
    abstained = models.BooleanField(default=False)
    abstention_reason = models.CharField(max_length=255, blank=True, default="")

    retrieved_documents = models.JSONField(blank=True, default=list)
    cited_documents = models.JSONField(blank=True, default=list)
    citations = models.JSONField(blank=True, default=list)

    # Per-case metric values, so a bad aggregate can be traced to the cases
    # that caused it rather than only observed.
    scores = models.JSONField(blank=True, default=dict)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["case_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "case"], name="unique_case_per_evaluation_run"
            )
        ]

    def __str__(self):
        return f"case {self.case_id} in run {self.run_id}"


class Feedback(models.Model):
    """A human verdict on one answer (spec §15, §20).

    Online quality signal, as opposed to the offline benchmark above.
    Kept against the Message rather than the conversation so it points at
    exactly the answer being judged, and stores the citation count and
    abstention state at the time it was given - a later reprocess changes
    the answer, and feedback about an answer that no longer exists would
    otherwise silently re-attach to a different one.
    """

    class Rating(models.TextChoices):
        HELPFUL = "HELPFUL", "Helpful"
        NOT_HELPFUL = "NOT_HELPFUL", "Not helpful"
        # Distinct from NOT_HELPFUL on purpose: "this is wrong" and "this
        # did not help me" are different failures with different fixes.
        INCORRECT = "INCORRECT", "Incorrect"

    message = models.ForeignKey(
        "documents.Message", on_delete=models.CASCADE, related_name="feedback"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="answer_feedback",
    )
    rating = models.CharField(max_length=20, choices=Rating.choices)
    comment = models.TextField(blank=True, default="")

    # Snapshot of what was being judged.
    answer_abstained = models.BooleanField(default=False)
    answer_citations = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "user"], name="one_feedback_per_user_per_message"
            )
        ]

    def __str__(self):
        return f"{self.rating} on message {self.message_id}"
