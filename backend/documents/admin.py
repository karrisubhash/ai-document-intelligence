from django.contrib import admin

from .models import (
    AuditLog,
    ClassificationResult,
    Document,
    DocumentContent,
    DocumentVersion,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)


class DocumentVersionInline(admin.TabularInline):
    model = DocumentVersion
    extra = 0
    fields = ("version_number", "file", "file_size", "checksum", "created_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
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
class DocumentVersionAdmin(admin.ModelAdmin):
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
class ProcessingJobAdmin(admin.ModelAdmin):
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
class ProcessingEventAdmin(admin.ModelAdmin):
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
class DocumentContentAdmin(admin.ModelAdmin):
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
class PageContentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "content",
        "page_number",
        "extraction_method",
        "character_count",
    )
    list_filter = ("extraction_method",)


@admin.register(ClassificationResult)
class ClassificationResultAdmin(admin.ModelAdmin):
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
class AuditLogAdmin(admin.ModelAdmin):
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
