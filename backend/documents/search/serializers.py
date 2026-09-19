from rest_framework import serializers

from documents.models import Document

MAX_QUERY_LENGTH = 1000


class SearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=MAX_QUERY_LENGTH, trim_whitespace=True)
    limit = serializers.IntegerField(
        required=False, default=10, min_value=1, max_value=50
    )
    document_type = serializers.ChoiceField(
        choices=Document.DocumentType.choices,
        required=False,
        allow_blank=True,
        default="",
    )
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


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
    matched_by = serializers.ListField(child=serializers.CharField())
    lexical_rank = serializers.IntegerField(allow_null=True)
    vector_rank = serializers.IntegerField(allow_null=True)
