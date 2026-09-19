from rest_framework import serializers

from documents.models import (
    ClassificationResult,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentContent,
    DocumentVersion,
    ExtractedField,
    ExtractionResult,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)
from documents.uploads import validate_upload


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


class UploadSerializer(serializers.Serializer):
    """Shared admission check for every path that stores a file.

    Both entry points - a new document and a new version of one - go
    through `documents.uploads.validate_upload` before anything is written.
    Leaving it on one of them would mean the rule could be walked around by
    posting to the other, which is the sort of asymmetry an access review
    finds and a test does not.
    """

    def validate_file(self, file_obj):
        request = self.context.get("request")
        return validate_upload(
            file_obj, user=getattr(request, "user", None) if request else None
        )


class DocumentCreateSerializer(UploadSerializer):
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


class DocumentVersionCreateSerializer(UploadSerializer):
    file = serializers.FileField()


class ClassificationResultSerializer(serializers.ModelSerializer):
    """One classifier verdict, with the model that produced it.

    Every run is returned, not just the newest: a verdict is only
    interpretable next to the model and version that made it, and a
    re-run under a newer model is a comparison, not a replacement.
    """

    class Meta:
        model = ClassificationResult
        fields = [
            "id",
            "document_content",
            "processing_job",
            "model_name",
            "model_version",
            "predicted_label",
            "confidence",
            "input_reference",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields


class ExtractedFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExtractedField
        fields = [
            "id",
            "field_name",
            "status",
            "value",
            "confidence",
            "page_number",
            "character_start",
            "character_end",
            "matched_text",
            "metadata",
        ]
        read_only_fields = fields


class ExtractionResultSerializer(serializers.ModelSerializer):
    extracted_fields = ExtractedFieldSerializer(many=True, read_only=True)

    class Meta:
        model = ExtractionResult
        fields = [
            "id",
            "document_content",
            "processing_job",
            "model_name",
            "model_version",
            "schema_name",
            "schema_version",
            "document_type",
            "field_count",
            "found_count",
            "input_reference",
            "metadata",
            "created_at",
            "extracted_fields",
        ]
        read_only_fields = fields


class ChunkEmbeddingSerializer(serializers.ModelSerializer):
    """The embedding's identity, never the vector itself.

    384 floats per chunk is a payload no reader can use, and `semantic`
    is the only part of it that changes how a search result should be
    read - so that is what travels.
    """

    class Meta:
        model = DocumentChunkEmbedding
        fields = [
            "id",
            "model_name",
            "model_version",
            "dimensions",
            "semantic",
            "created_at",
        ]
        read_only_fields = fields


class DocumentChunkSerializer(serializers.ModelSerializer):
    embeddings = ChunkEmbeddingSerializer(many=True, read_only=True)

    class Meta:
        model = DocumentChunk
        fields = [
            "id",
            "chunk_index",
            "page_number",
            "character_start",
            "character_end",
            "text",
            "character_count",
            "chunking_strategy",
            "chunking_version",
            "metadata",
            "created_at",
            "embeddings",
        ]
        read_only_fields = fields
