from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    class Action(models.TextChoices):
        DOCUMENT_VIEW = "DOCUMENT_VIEW", "Document view"
        DOCUMENT_CREATE = "DOCUMENT_CREATE", "Document create"
        DOCUMENT_UPDATE = "DOCUMENT_UPDATE", "Document update"
        DOCUMENT_DELETE = "DOCUMENT_DELETE", "Document delete"
        DOCUMENT_DOWNLOAD = "DOCUMENT_DOWNLOAD", "Document download"
        DOCUMENT_REPROCESS = "DOCUMENT_REPROCESS", "Document reprocess"
        DOCUMENT_VERSION_UPLOAD = "DOCUMENT_VERSION_UPLOAD", "Document version upload"
        DOCUMENT_ARCHIVE = "DOCUMENT_ARCHIVE", "Document archive"
        DOCUMENT_RESTORE = "DOCUMENT_RESTORE", "Document restore"
        SEARCH = "SEARCH", "Search"
        CHAT = "CHAT", "Chat"
        # Identity. A failed login is recorded against the account it
        # targeted when one exists, so a guessing run against one person
        # shows up on that person's trail.
        LOGIN = "LOGIN", "Login"
        LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
        LOGOUT = "LOGOUT", "Logout"
        PASSWORD_CHANGE = "PASSWORD_CHANGE", "Password change"
        # Administration. Every change to who can see what is itself
        # something an auditor needs to be able to see.
        USER_CREATE = "USER_CREATE", "User create"
        USER_UPDATE = "USER_UPDATE", "User update"
        DEPARTMENT_CREATE = "DEPARTMENT_CREATE", "Department create"
        DEPARTMENT_UPDATE = "DEPARTMENT_UPDATE", "Department update"
        JOB_RETRY = "JOB_RETRY", "Processing job retry"

    class Result(models.TextChoices):
        ALLOWED = "ALLOWED", "Allowed"
        DENIED = "DENIED", "Denied"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    document = models.ForeignKey(
        "documents.Document",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    result = models.CharField(max_length=10, choices=Result.choices)
    metadata = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The fastest-growing table in the schema - every view, search
            # and chat appends a row - and the administration audit screen
            # reads it newest-first, paginated. Without an index on the sort
            # key every page is a sort of the whole table.
            models.Index(fields=["-created_at"], name="auditlog_created_idx"),
            # "What did this person do", the question an investigation
            # starts with.
            models.Index(fields=["user", "-created_at"], name="auditlog_user_idx"),
        ]

    def __str__(self):
        return f"{self.user} {self.action} {self.result}"
