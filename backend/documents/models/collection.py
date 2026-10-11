from django.conf import settings
from django.db import models


class Collection(models.Model):
    """A named set of documents to search and ask questions over (spec 14, 15.2).

    A collection NARROWS, it never grants. Search and chat apply the
    caller's document authorization first and the collection after it, so a
    document in a collection its reader cannot otherwise see stays invisible
    - the same rule every other filter follows (documents.search.service).

    Scoped to a department like documents are, and only holds documents of
    that department, so a collection can never carry text across the
    tenant boundary.
    """

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    department = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    documents = models.ManyToManyField(
        "documents.Document", related_name="collections", blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "name"],
                name="unique_collection_name_per_department",
            )
        ]

    def __str__(self):
        return self.name
