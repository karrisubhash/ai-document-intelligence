from django.conf import settings
from rest_framework import serializers

from documents.models import Document

MAX_QUERY_LENGTH = 1000


def max_limit() -> int:
    return max(1, int(getattr(settings, "SEARCH_MAX_LIMIT", 50)))


def default_limit() -> int:
    return min(max(1, int(getattr(settings, "SEARCH_DEFAULT_LIMIT", 10))), max_limit())


class SearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=MAX_QUERY_LENGTH, trim_whitespace=True)
    # Bounded by SEARCH_MAX_LIMIT and defaulted from SEARCH_DEFAULT_LIMIT at
    # validation time rather than import time, so both are configuration.
    limit = serializers.IntegerField(required=False, min_value=1)
    document_type = serializers.ChoiceField(
        choices=Document.DocumentType.choices,
        required=False,
        allow_blank=True,
        default="",
    )
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    document_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    # Narrows to one collection, after authorization (spec 14).
    collection_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    # Also return the passages the reranker scored below the relevance
    # floor - after every passage above it, and marked `below_floor`.
    # `limit` caps the passages above the floor; every weaker one is
    # appended, so a response can hold more than `limit` passages.
    include_below_floor = serializers.BooleanField(required=False, default=False)
    # "document": the response also carries `groups` - the passages above
    # the floor, one entry per document (search.presentation). `results`
    # is returned either way.
    group_by = serializers.ChoiceField(
        choices=[("", "none"), ("document", "document")],
        required=False,
        allow_blank=True,
        default="",
    )

    def validate_limit(self, value):
        ceiling = max_limit()
        if value > ceiling:
            raise serializers.ValidationError(
                f"Ensure this value is less than or equal to {ceiling}."
            )
        return value

    def validate(self, attrs):
        attrs.setdefault("limit", default_limit())
        return attrs


class SearchHitSerializer(serializers.Serializer):
    """A hit is a citation plus why it was retrieved."""

    document_id = serializers.IntegerField()
    document_title = serializers.CharField()
    page_number = serializers.IntegerField()
    chunk_id = serializers.IntegerField()
    character_start = serializers.IntegerField()
    character_end = serializers.IntegerField()
    text = serializers.CharField()
    score = serializers.FloatField()
    relevance = serializers.FloatField(allow_null=True)
    below_floor = serializers.BooleanField()
    matched_by = serializers.ListField(child=serializers.CharField())
    lexical_rank = serializers.IntegerField(allow_null=True)
    vector_rank = serializers.IntegerField(allow_null=True)
