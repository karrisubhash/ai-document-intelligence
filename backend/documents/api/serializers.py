from rest_framework import serializers

from documents import authorization
from documents.api import discovery
from documents.models import (
    ClassificationResult,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentContent,
    DocumentFigure,
    DocumentTable,
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
    owner_username = serializers.SerializerMethodField()
    is_archived = serializers.BooleanField(read_only=True)
    # What the CALLER may do with this document. A hint for the interface -
    # which buttons to show - and nothing more: every action is decided
    # again by DocumentAccessPolicy when it is attempted.
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "document_type",
            "department",
            "owner",
            "owner_username",
            "status",
            "current_version",
            "is_archived",
            "archived_at",
            "can_manage",
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
            "archived_at",
            "created_at",
            "updated_at",
        ]

    def get_owner_username(self, document):
        return document.owner.get_username() if document.owner_id else ""

    def get_can_manage(self, document):
        request = self.context.get("request")
        if request is None:
            return False
        return authorization.can_manage_document(request.user, document)


class DocumentVersionSummarySerializer(serializers.ModelSerializer):
    """Just enough of a version to browse and size it in a results list.

    Deliberately narrower than DocumentVersionSerializer: no `file` (the
    storage key - relative filesystem path locally, or the S3 object key -
    which a discovery listing has no use for and §16 asks not to expose),
    and no `checksum` (integrity metadata for verifying a download, not for
    browsing). Both remain on the version's own endpoint
    (`/versions/`) and on document detail, where a caller with a specific
    reason to check them still can.
    """

    class Meta:
        model = DocumentVersion
        fields = [
            "id",
            "version_number",
            "file_name",
            "file_size",
            "mime_type",
            "uploaded_by",
            "created_at",
        ]
        read_only_fields = fields


class DocumentListSerializer(serializers.ModelSerializer):
    """The `list` action's shape - see DocumentVersionSummarySerializer.

    Every top-level field here is identical to DocumentSerializer, so a
    consumer reading `results[].id/title/document_type/...` sees no change;
    only the nested `current_version` is slimmer. That is the one
    intentional, documented difference from the detail endpoint's shape
    (README, "Document discovery").
    """

    current_version = DocumentVersionSummarySerializer(read_only=True)
    owner_username = serializers.SerializerMethodField()
    is_archived = serializers.BooleanField(read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "document_type",
            "department",
            "owner",
            "owner_username",
            "status",
            "current_version",
            "is_archived",
            "archived_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_owner_username(self, document):
        return document.owner.get_username() if document.owner_id else ""


class DocumentListQuerySerializer(serializers.Serializer):
    """Query parameters for GET /api/documents/.

    Modeled on documents.search.serializers.SearchRequestSerializer: typed
    fields validate before a single query is built, so an invalid value
    fails with the same `{"field": ["..."]}` shape every other endpoint
    uses - never a 500 from an unparsed int() or a filter that silently
    matched nothing because the value made no sense.

    Absent or blank means "no filter" for every field here, matching how
    the search endpoint already treats an optional `document_type`/
    `department`. Validated values are handed to `documents.api.discovery
    .apply()`, which is the only thing that turns them into a queryset -
    this class only says what a caller may ask for, never runs a query.
    """

    search = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        default="",
    )
    status = serializers.ChoiceField(
        choices=Document.Status.choices, required=False, allow_blank=True, default=""
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
    # An integer user id, or the literal "me" - resolved to request.user's
    # own id in validate_owner, so nothing downstream ever sees the string.
    owner = serializers.CharField(required=False, allow_blank=True, default="")
    created_after = serializers.CharField(required=False, allow_blank=True, default="")
    created_before = serializers.CharField(required=False, allow_blank=True, default="")
    updated_after = serializers.CharField(required=False, allow_blank=True, default="")
    updated_before = serializers.CharField(required=False, allow_blank=True, default="")
    ordering = serializers.CharField(required=False, allow_blank=True, default="")
    # Archived documents are out of the default listing, the way they are
    # out of search. "true" lists only them; "all" lists both.
    archived = serializers.ChoiceField(
        choices=["false", "true", "all"],
        required=False,
        allow_blank=True,
        default="false",
    )

    def validate_owner(self, value):
        if value == "":
            return None
        if value == "me":
            request = self.context.get("request")
            return request.user.id
        # isdecimal() rather than isdigit(), and int() guarded: "--5" and
        # "²" passed the old isdigit() test and then crashed int(), as does
        # anything past Python's 4,300-digit int() limit - each a 500.
        if value.isdecimal():
            try:
                return int(value)
            except ValueError:
                pass
        raise serializers.ValidationError(
            "Must be a non-negative integer user id, or 'me'."
        )

    def validate_created_after(self, value):
        return (
            discovery.parse_boundary(value, "created_after", end_of_day=False)
            if value
            else None
        )

    def validate_created_before(self, value):
        return (
            discovery.parse_boundary(value, "created_before", end_of_day=True)
            if value
            else None
        )

    def validate_updated_after(self, value):
        return (
            discovery.parse_boundary(value, "updated_after", end_of_day=False)
            if value
            else None
        )

    def validate_updated_before(self, value):
        return (
            discovery.parse_boundary(value, "updated_before", end_of_day=True)
            if value
            else None
        )

    def validate_ordering(self, value):
        return discovery.resolve_ordering(value) if value else None

    def validate(self, attrs):
        after, before = attrs.get("created_after"), attrs.get("created_before")
        if after is not None and before is not None and after > before:
            raise serializers.ValidationError(
                {"created_before": ["Must not be earlier than created_after."]}
            )

        after, before = attrs.get("updated_after"), attrs.get("updated_before")
        if after is not None and before is not None and after > before:
            raise serializers.ValidationError(
                {"updated_before": ["Must not be earlier than updated_after."]}
            )

        return attrs

    def to_internal_value(self, data):
        # Renamed on the way out so discovery.apply() never has to know the
        # public query parameter was "owner" - it reads owner_id, matching
        # the model column it filters on.
        validated = super().to_internal_value(data)
        validated["owner_id"] = validated.pop("owner")
        return validated


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


class DocumentTableSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentTable
        fields = [
            "id",
            "table_index",
            "page_number",
            "character_start",
            "character_end",
            "header",
            "rows",
            "row_count",
            "column_count",
            "detector",
            "detector_version",
        ]
        read_only_fields = fields


class DocumentFigureSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentFigure
        fields = [
            "id",
            "figure_index",
            "page_number",
            "width",
            "height",
            "description",
            "description_source",
            "detector",
            "detector_version",
        ]
        read_only_fields = fields
