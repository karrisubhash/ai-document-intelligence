from rest_framework import serializers

from documents.models import AuditLog, ProcessingJob


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    document = serializers.SerializerMethodField()
    document_title = serializers.SerializerMethodField()
    # True when a document is involved but belongs to another department, so
    # the screen can say so without the id.
    document_withheld = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "created_at",
            "action",
            "result",
            "user",
            "username",
            "document",
            "document_title",
            "document_withheld",
            "metadata",
        ]
        read_only_fields = fields

    def get_username(self, entry):
        return entry.user.get_username() if entry.user_id else ""

    def _outside_scope(self, entry) -> bool:
        scope = self.context.get("department_scope")
        return scope is not None and entry.document.department != scope

    def get_document(self, entry):
        """Withheld, like the title, for another department's document.

        "The id is opaque" was not true: a DENIED row is written only for an
        id that EXISTS (a missing one 404s before the policy runs), so a
        department admin who probed ids got a 404 for every one and then read
        in their own audit trail exactly which ones were another
        department's documents - the existence leak the 404 exists to stop.
        """
        if entry.document_id is None or self._outside_scope(entry):
            return None
        return entry.document_id

    def get_document_withheld(self, entry) -> bool:
        return entry.document_id is not None and self._outside_scope(entry)

    def get_document_title(self, entry):
        """Withheld from a department admin for another department's document.

        A row reaches a department admin because one of THEIR members acted
        - including being denied a document elsewhere. The id is already
        opaque; the title is not, and "your student tried to open 'Salary
        Revisions 2025'" discloses another department's document to someone
        who may not read it. A system admin sees every title.
        """
        if entry.document_id is None or self._outside_scope(entry):
            return ""
        return entry.document.title


class OperationsJobSerializer(serializers.ModelSerializer):
    document_id = serializers.IntegerField(
        source="document_version.document_id", read_only=True
    )
    document_title = serializers.CharField(
        source="document_version.document.title", read_only=True
    )
    department = serializers.CharField(
        source="document_version.document.department", read_only=True
    )
    version_number = serializers.IntegerField(
        source="document_version.version_number", read_only=True
    )
    is_current_version = serializers.SerializerMethodField()
    is_stalled = serializers.SerializerMethodField()

    class Meta:
        model = ProcessingJob
        fields = [
            "id",
            "document_id",
            "document_title",
            "department",
            "version_number",
            "is_current_version",
            "status",
            "current_stage",
            "attempt",
            "error_code",
            "error_message",
            "is_stalled",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_is_current_version(self, job):
        return (
            job.document_version.document.current_version_id == job.document_version_id
        )

    def get_is_stalled(self, job):
        cutoff = self.context.get("cutoff")
        return bool(
            cutoff
            and job.status == ProcessingJob.Status.PROCESSING
            and job.updated_at < cutoff
        )
