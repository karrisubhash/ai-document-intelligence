from rest_framework import serializers

from documents.models import (
    Document,
    DocumentContent,
    DocumentVersion,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)


class PageContentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageContent
        fields = [
            "page_number",
            "text",
            "extraction_method",
            "character_count",
            "metadata",
        ]
        read_only_fields = fields


class DocumentContentSerializer(serializers.ModelSerializer):
    pages = PageContentSerializer(many=True, read_only=True)

    class Meta:
        model = DocumentContent
        fields = [
            "id",
            "document_version",
            "processing_job",
            "processing_version",
            "page_count",
            "metadata",
            "created_at",
            "pages",
        ]
        read_only_fields = fields


class DocumentVersionSerializer(serializers.ModelSerializer):
    # Storage key, not a fetchable URL - the bytes are only served by the
    # authenticated /api/documents/{id}/download/ endpoint.
    file = serializers.FileField(use_url=False)

    class Meta:
        model = DocumentVersion
        fields = [
            "id",
            "version_number",
            "file",
            "file_name",
            "file_size",
            "mime_type",
            "checksum",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class ProcessingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingEvent
        fields = [
            "id",
            "stage",
            "status",
            "message",
            "metadata",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = fields


class ProcessingJobSerializer(serializers.ModelSerializer):
    events = ProcessingEventSerializer(many=True, read_only=True)

    class Meta:
        model = ProcessingJob
        fields = [
            "id",
            "document_version",
            "status",
            "current_stage",
            "attempt",
            "error_code",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
            "events",
        ]
        read_only_fields = fields


class DocumentSerializer(serializers.ModelSerializer):
    current_version = DocumentVersionSerializer(read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "document_type",
            "department",
            "owner",
            "status",
            "current_version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            # Department is a security boundary (see documents.authorization) -
            # it's set from the uploader's profile at creation time, not
            # editable afterwards through this serializer.
            "department",
            "owner",
            "status",
            "current_version",
            "created_at",
            "updated_at",
        ]


class DocumentCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    document_type = serializers.ChoiceField(
        choices=Document.DocumentType.choices,
        required=False,
        default=Document.DocumentType.GENERAL,
    )
    department = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        default="",
    )
    file = serializers.FileField()


class DocumentVersionCreateSerializer(serializers.Serializer):
    file = serializers.FileField()
