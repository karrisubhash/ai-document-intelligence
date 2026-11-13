from django.db.models import Exists, F, OuterRef, Subquery
from rest_framework import serializers

from documents import authorization
from documents.api import discovery
from documents.models import (
    ClassificationResult,
    Collection,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentContent,
    DocumentFigure,
    DocumentTable,
    DocumentThumbnail,
    DocumentVersion,
    ExtractedField,
    ExtractionResult,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)
from documents.uploads import validate_upload

IN_FLIGHT = (
    Document.Status.UPLOADED,
    Document.Status.QUEUED,
    Document.Status.PROCESSING,
)
ACTIVE_JOB = (ProcessingJob.Status.QUEUED, ProcessingJob.Status.PROCESSING)


def _thumbnail_exists():
    # Only for a version with a live pass: a quarantined version's pass is
    # withdrawn, and its picture with it (the endpoint answers 404).
    return Exists(
        DocumentThumbnail.objects.filter(
            document_version=OuterRef("current_version"),
            document_version__current_content__isnull=False,
        )
    )


def _active_stage():
    return Subquery(
        ProcessingJob.objects.filter(
            document_version=OuterRef("current_version"), status__in=ACTIVE_JOB
        )
        .order_by("-created_at")
        .values("current_stage")[:1]
    )


def with_listing_facts(queryset):
    """Annotate what the listing shows per row, in the listing's one query.

    Page count, whether a thumbnail exists and the stage an in-flight job
    is at - each a join or a correlated subquery, never a query per row
    (QueryEfficiencyTests holds the listing to that).
    """
    return queryset.annotate(
        listing_page_count=F("current_version__current_content__page_count"),
        listing_has_thumbnail=_thumbnail_exists(),
        listing_stage=_active_stage(),
    )


def display_name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name().strip() or user.get_username()


class DocumentFactsMixin(serializers.Serializer):
    """Facts shared by the list and detail shapes, from annotations when the
    listing provides them and with a query each on a single document."""

    page_count = serializers.SerializerMethodField()
    has_thumbnail = serializers.SerializerMethodField()
    current_stage = serializers.SerializerMethodField()

    def get_page_count(self, document):
        if hasattr(document, "listing_page_count"):
            return document.listing_page_count
        if not document.current_version_id:
            return None
        return (
            DocumentContent.objects.filter(
                pk=document.current_version.current_content_id
            )
            .values_list("page_count", flat=True)
            .first()
        )

    def get_has_thumbnail(self, document):
        if hasattr(document, "listing_has_thumbnail"):
            return document.listing_has_thumbnail
        return (
            Document.objects.filter(pk=document.pk).filter(_thumbnail_exists()).exists()
        )

    def get_current_stage(self, document):
        # Only while in flight: a finished document is at no stage.
        if document.status not in IN_FLIGHT:
            return None
        if hasattr(document, "listing_stage"):
            return document.listing_stage or None
        stage = (
            ProcessingJob.objects.filter(
                document_version_id=document.current_version_id,
                status__in=ACTIVE_JOB,
            )
            .order_by("-created_at")
            .values_list("current_stage", flat=True)
            .first()
        )
        return stage or None


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
    """A version as document detail and the Versions tab show it.

    No `file`: that is the storage key - a relative filesystem path, or the
    S3 object key - an internal detail no client used (the bytes are only
    served by the authenticated /download/ endpoint) and §16 asks not to
    expose. It was already withheld from the listing; detail, create,
    archive and /versions/ now match. `checksum` stays: it is what a caller
    verifies a download against.
    """

    # Who replaced the file, readably - the id alone told a reader nothing.
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = DocumentVersion
        fields = [
            "id",
            "version_number",
            "file_name",
            "file_size",
            "mime_type",
            "checksum",
            "uploaded_by",
            "uploaded_by_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_uploaded_by_name(self, version):
        return display_name(version.uploaded_by) if version.uploaded_by_id else ""


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


class DocumentSerializer(DocumentFactsMixin, serializers.ModelSerializer):
    current_version = DocumentVersionSerializer(read_only=True)
    owner_username = serializers.SerializerMethodField()
    is_archived = serializers.BooleanField(read_only=True)
    # What the CALLER may do with this document. A hint for the interface -
    # which buttons to show - and nothing more: every action is decided
    # again by DocumentAccessPolicy when it is attempted.
    can_manage = serializers.SerializerMethodField()
    # Detail only: what the tabs hold, so empty ones can say so up front.
    counts = serializers.SerializerMethodField()
    collections = serializers.SerializerMethodField()
    # PATCH only, optional: the type the editing form showed when the person
    # chose a new one. A change made against a type the document no longer
    # has is refused (DocumentViewSet.perform_update). Never returned.
    expected_document_type = serializers.ChoiceField(
        choices=Document.DocumentType.choices, required=False, write_only=True
    )

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "document_type",
            "document_type_source",
            "type_confidence",
            "department",
            "owner",
            "owner_username",
            "status",
            "current_stage",
            "current_version",
            "page_count",
            "has_thumbnail",
            "counts",
            "collections",
            "is_archived",
            "archived_at",
            "can_manage",
            "created_at",
            "updated_at",
            "expected_document_type",
        ]
        read_only_fields = [
            "id",
            # Department is a security boundary (see documents.authorization) -
            # it's set from the uploader's profile at creation time, not
            # editable afterwards through this serializer.
            "department",
            # Who decided the type is derived from how it was set: a PATCH of
            # document_type marks it "user" (DocumentViewSet.perform_update).
            "document_type_source",
            "type_confidence",
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

    def get_counts(self, document):
        counts = {"tables": 0, "figures": 0, "fields_found": 0}
        version = document.current_version
        content_id = version.current_content_id if version else None
        if content_id is None:
            return counts
        counts["tables"] = DocumentTable.objects.filter(
            document_content_id=content_id
        ).count()
        counts["figures"] = DocumentFigure.objects.filter(
            document_content_id=content_id
        ).count()
        # Fields of the newest extraction under the CURRENT type - the one
        # the Fields tab shows; an older pass under another type does not
        # describe this document any more.
        counts["fields_found"] = (
            ExtractionResult.objects.filter(
                document_content_id=content_id, document_type=document.document_type
            )
            .order_by("-created_at")
            .values_list("found_count", flat=True)
            .first()
        ) or 0
        return counts

    def get_collections(self, document):
        request = self.context.get("request")
        if request is None:
            return []
        # Only collections the caller can see: a collection narrows, it
        # never discloses.
        visible = authorization.visible_collections(
            request.user, Collection.objects.filter(documents=document)
        )
        return [
            {"id": pk, "name": name} for pk, name in visible.values_list("pk", "name")
        ]


class DocumentVersionSummarySerializer(serializers.ModelSerializer):
    """Just enough of a version to browse and size it in a results list.

    Deliberately narrower than DocumentVersionSerializer: no `checksum`
    (integrity metadata for verifying a download, not for browsing) and no
    uploader name. The storage key (`file`) is exposed nowhere.
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


class DocumentListSerializer(DocumentFactsMixin, serializers.ModelSerializer):
    """The `list` action's shape - see DocumentVersionSummarySerializer.

    Every top-level field here is identical to DocumentSerializer, so a
    consumer reading `results[].id/title/document_type/...` sees no change;
    only the nested `current_version` is slimmer, and the detail-only
    `counts` and `collections` are absent (each would be a query per row).
    That is the intentional, documented difference from the detail
    endpoint's shape (README, "Document discovery").
    """

    current_version = DocumentVersionSummarySerializer(read_only=True)
    owner_username = serializers.SerializerMethodField()
    is_archived = serializers.BooleanField(read_only=True)
    can_manage = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "document_type",
            "document_type_source",
            "type_confidence",
            "department",
            "owner",
            "owner_username",
            "status",
            "current_stage",
            "current_version",
            "page_count",
            "has_thumbnail",
            "is_archived",
            "archived_at",
            "can_manage",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_owner_username(self, document):
        return document.owner.get_username() if document.owner_id else ""

    def get_can_manage(self, document):
        # Which rows the listing offers to select for bulk delete - a UI
        # hint only, as on DocumentSerializer; the delete decides again.
        request = self.context.get("request")
        if request is None:
            return False
        return authorization.can_manage_document(request.user, document)


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


# One request deletes at most this many documents. A document's delete
# cascades through its versions, pages, chunks and embeddings, so an
# unbounded request could outlive the worker timeout half-way through;
# "delete everything matching" is a client loop over `remaining` instead.
BULK_DELETE_LIMIT = 100


def _only_with_all(attrs, *names):
    for name in names:
        if attrs.get(name):
            raise serializers.ValidationError({name: ["Only applies with all: true."]})


class BulkSelectionSerializer(serializers.Serializer):
    """What a bulk action acts on: exactly one of `ids` (the rows the caller
    selected, at most BULK_DELETE_LIMIT) or `all: true`. Shared by every
    bulk-delete endpoint - documents, collections and conversations.

    With `all`, one request deletes at most BULK_DELETE_LIMIT and answers
    with `remaining` and an `as_of` instant. The client repeats, passing
    `as_of` back, so a row created after the first request - by a colleague,
    or in another tab - is never swept up by a delete nobody confirmed.

    Unknown keys are refused, not ignored: on a delete, an option that is
    silently dropped (a misspelt filter) WIDENS what is deleted.
    """

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        max_length=BULK_DELETE_LIMIT,
    )
    all = serializers.BooleanField(required=False, default=False)
    as_of = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {name: ["Unknown field."] for name in sorted(unknown)}
            )
        if bool(attrs.get("ids")) == attrs["all"]:
            raise serializers.ValidationError(
                "Send either a list of ids or all: true, not both."
            )
        if not attrs["all"]:
            _only_with_all(attrs, "as_of")
        return attrs


class BulkDeleteSerializer(BulkSelectionSerializer):
    """Body of POST /api/v1/documents/bulk-delete/.

    With `all: true`, every document the caller may delete that matches
    `filters`, which takes the listing's query parameters (validated by
    DocumentListQuerySerializer, so "all" means what the listing showed).
    `preview: true` deletes nothing and answers the counts and `as_of` - what
    a confirmation dialog shows, and the snapshot the delete then keeps to.
    """

    filters = serializers.DictField(required=False, default=dict)
    preview = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs["all"]:
            _only_with_all(attrs, "filters", "preview")
            return attrs
        # DocumentListQuerySerializer, like any serializer, drops keys it
        # does not declare; for the listing that only shows more rows.
        unknown = set(attrs["filters"]) - set(DocumentListQuerySerializer().fields)
        if unknown:
            raise serializers.ValidationError(
                {"filters": [f"Unknown filter: {name}." for name in sorted(unknown)]}
            )
        query = DocumentListQuerySerializer(data=attrs["filters"], context=self.context)
        if not query.is_valid():
            raise serializers.ValidationError({"filters": query.errors})
        attrs["filters"] = query.validated_data
        return attrs


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
            # "document_type": the product's own type, from the type
            # detector. "layout_class": a page-layout label (RVL-CDIP) - to
            # be shown as exactly that, never as the document's type.
            "taxonomy",
            "predicted_label",
            "confidence",
            # The classifier declined to name a label (predicted_label is
            # then "unknown"; metadata.top_label holds its best guess).
            "abstained",
            "input_reference",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # The model artifact's location on the server is an operator's
        # detail, not something a reader needs or should see.
        if isinstance(data.get("metadata"), dict):
            data["metadata"] = {
                key: value
                for key, value in data["metadata"].items()
                if key != "artifact"
            }
        return data


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
