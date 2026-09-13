from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """A thread of questions against the documents a user may read.

    Owned by a user and stamped with a department at creation, because the
    answers inside it were produced under that user's authorization. A
    conversation is a record of what was disclosed, so it inherits the
    boundary that governed the disclosure rather than re-deriving it later
    from a profile that may since have changed.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    title = models.CharField(max_length=255, blank=True, default="")
    department = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Conversation {self.id}"


class Message(models.Model):
    """One turn. Assistant turns keep everything needed to audit the answer.

    The citations, the abstention reason, the models used and the grounding
    score are stored rather than recomputed, because an answer is only
    auditable if the evidence it was built from is the evidence on record -
    and re-running retrieval later would consult a different index.
    """

    class Role(models.TextChoices):
        USER = "USER", "User"
        ASSISTANT = "ASSISTANT", "Assistant"

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()

    # Assistant turns only.
    abstained = models.BooleanField(default=False)
    abstention_reason = models.CharField(max_length=255, blank=True, default="")
    # Full section 12.3 citations, resolved at answer time.
    citations = models.JSONField(blank=True, default=list)
    support_ratio = models.FloatField(default=0.0)

    model_name = models.CharField(max_length=100, blank=True, default="")
    model_version = models.CharField(max_length=50, blank=True, default="")
    # Whether the answer was generated or extracted verbatim. A reader
    # should not have to guess which kind of answer they are looking at.
    generative = models.BooleanField(default=False)

    retrieved = models.PositiveIntegerField(default=0)
    passages_used = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)

    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["conversation", "created_at"])]

    def __str__(self):
        return f"{self.role}: {self.content[:50]}"
