from rest_framework import permissions

from . import authorization
from .audit import record_event
from .models import AuditLog

# Actions that only ever read a document. "versions" is mixed (GET lists,
# POST adds one) and is handled separately below.
READ_ONLY_ACTIONS = {"retrieve", "processing", "download", "content"}

ACTION_TO_AUDIT_EVENT = {
    "retrieve": AuditLog.Action.DOCUMENT_VIEW,
    "update": AuditLog.Action.DOCUMENT_UPDATE,
    "partial_update": AuditLog.Action.DOCUMENT_UPDATE,
    "destroy": AuditLog.Action.DOCUMENT_DELETE,
    "reprocess": AuditLog.Action.DOCUMENT_REPROCESS,
    "download": AuditLog.Action.DOCUMENT_DOWNLOAD,
    # Extracted text is the document's substance - reading it is a view.
    "content": AuditLog.Action.DOCUMENT_VIEW,
}


class DocumentAccessPolicy(permissions.BasePermission):
    """Object-level access control for documents, with audit logging.

    Read: document owner, someone in the document's department, or a
    system admin. Manage (update/delete/reprocess/new version): owner,
    a department admin of that department, or a system admin.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, document):
        is_read = view.action in READ_ONLY_ACTIONS or (
            view.action == "versions" and request.method in permissions.SAFE_METHODS
        )

        allowed = (
            authorization.can_view_document(request.user, document)
            if is_read
            else authorization.can_manage_document(request.user, document)
        )

        action = self._audit_action(view, request)
        if action:
            record_event(
                user=request.user,
                action=action,
                document=document,
                result=AuditLog.Result.ALLOWED if allowed else AuditLog.Result.DENIED,
            )

        return allowed

    @staticmethod
    def _audit_action(view, request):
        if view.action == "versions":
            return (
                AuditLog.Action.DOCUMENT_VERSION_UPLOAD
                if request.method == "POST"
                else AuditLog.Action.DOCUMENT_VIEW
            )
        return ACTION_TO_AUDIT_EVENT.get(view.action)
