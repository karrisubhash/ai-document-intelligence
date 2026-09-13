from django.contrib import admin
from django.db import models

from . import authorization
from .models import (
    AuditLog,
    ClassificationResult,
    Document,
    DocumentContent,
    DocumentVersion,
    ExtractedField,
    ExtractionResult,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)


class DepartmentScopedAdmin(admin.ModelAdmin):
    """Applies the same tenant rules the API does.

    `is_staff` only means "may open the admin"; it is not a statement that
    someone may read every department's documents. Without this the admin
    is a complete bypass of documents.authorization - the one place the
    rules are supposed to live - for anyone who can reach /admin/.

    Subclasses declare how to reach Document from their own model, so the
    single rule in `authorization` keeps being the only rule.
    """

    # Lookup path from this model to Document, e.g. "document_version__document".
    document_path = None

    def get_queryset(self, request):
        queryset = super().get_queryset(request)

        if authorization.is_system_admin(request.user):
            return queryset

        profile = authorization.get_profile(request.user)
        prefix = f"{self.document_path}__" if self.document_path else ""

        visible = models.Q(**{f"{prefix}owner": request.user})
        if profile.department:
            visible |= models.Q(**{f"{prefix}department": profile.department})

        return queryset.filter(visible)


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    fields = ("version_number", "file", "file_size", "checksum", "created_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Document)
class DocumentAdmin(DepartmentScopedAdmin):
    document_path = None
    list_display = (
        "id",
        "title",
        "document_type",
        "department",
        "owner",
        "status",
        "current_version",
        "created_at",
    )
    list_filter = ("status", "document_type")
    search_fields = ("title", "department")
    inlines = [DocumentVersionInline]


@admin.register(DocumentVersion)
class DocumentVersionAdmin(DepartmentScopedAdmin):
    document_path = "document"
    list_display = (
        "id",
        "document",
        "version_number",
        "file_name",
        "file_size",
        "checksum",
        "created_at",
    )
    list_filter = ("mime_type",)
    search_fields = ("file_name", "checksum", "document__title")


class ProcessingEventInline(admin.TabularInline):
    model = ProcessingEvent
    extra = 0
    fields = ("stage", "status", "message", "started_at", "completed_at")
    readonly_fields = fields
    can_delete = False


@admin.register(ProcessingJob)
class ProcessingJobAdmin(DepartmentScopedAdmin):
    document_path = "document_version__document"
    list_display = (
        "id",
        "document_version",
        "status",
        "current_stage",
        "attempt",
        "error_code",
        "created_at",
    )
    list_filter = ("status", "current_stage")
    inlines = [ProcessingEventInline]


@admin.register(ProcessingEvent)
class ProcessingEventAdmin(DepartmentScopedAdmin):
    document_path = "processing_job__document_version__document"
    list_display = (
        "id",
        "processing_job",
        "stage",
        "status",
        "created_at",
    )
    list_filter = ("stage", "status")


class PageContentInline(admin.TabularInline):
    model = PageContent
    extra = 0
    fields = ("page_number", "extraction_method", "character_count", "metadata")
    readonly_fields = fields
    can_delete = False


@admin.register(DocumentContent)
class DocumentContentAdmin(DepartmentScopedAdmin):
    document_path = "document_version__document"
    list_display = (
        "id",
        "document_version",
        "processing_version",
        "page_count",
        "created_at",
    )
    list_filter = ("processing_version",)
    inlines = [PageContentInline]


@admin.register(PageContent)
class PageContentAdmin(DepartmentScopedAdmin):
    document_path = "content__document_version__document"
    list_display = (
        "id",
        "content",
        "page_number",
        "extraction_method",
        "character_count",
    )
    list_filter = ("extraction_method",)


@admin.register(ClassificationResult)
class ClassificationResultAdmin(DepartmentScopedAdmin):
    document_path = "document_version__document"
    list_display = (
        "id",
        "document_version",
        "predicted_label",
        "confidence",
        "model_name",
        "model_version",
        "created_at",
    )
    list_filter = ("model_name", "model_version", "predicted_label")
    search_fields = ("predicted_label", "document_version__document__title")


@admin.register(AuditLog)
class AuditLogAdmin(DepartmentScopedAdmin):
    document_path = "document"
    list_display = (
        "id",
        "user",
        "action",
        "document",
        "result",
        "created_at",
    )
    list_filter = ("action", "result")
    search_fields = ("user__username", "document__title")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ExtractedFieldInline(admin.TabularInline):
    model = ExtractedField
    extra = 0
    fields = (
        "field_name",
        "status",
        "value",
        "confidence",
        "page_number",
        "character_start",
        "character_end",
    )
    readonly_fields = fields
    can_delete = False


@admin.register(ExtractionResult)
class ExtractionResultAdmin(DepartmentScopedAdmin):
    document_path = "document_version__document"
    list_display = (
        "id",
        "document_version",
        "document_type",
        "schema_name",
        "schema_version",
        "found_count",
        "field_count",
        "created_at",
    )
    list_filter = ("schema_name", "schema_version", "document_type")
    inlines = [ExtractedFieldInline]


@admin.register(ExtractedField)
class ExtractedFieldAdmin(DepartmentScopedAdmin):
    document_path = "result__document_version__document"
    list_display = (
        "id",
        "result",
        "field_name",
        "status",
        "value",
        "page_number",
    )
    list_filter = ("status", "field_name")
    search_fields = ("field_name", "value")
