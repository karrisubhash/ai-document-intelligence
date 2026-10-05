from django.http import Http404
from rest_framework import permissions

from documents.observability import telemetry

from . import authorization
from .audit import record_event
from .models import AuditLog

# Actions that only ever read a document. "versions" is mixed (GET lists,
# POST adds one) and is handled separately below.
READ_ONLY_ACTIONS = {
    "retrieve",
    "processing",
    "download",
    "content",
    "classification",
    "extraction",
    "chunks",
    "tables",
    "figures",
    "source",
}

# Exactly the message DRF's own get_object() produces for an id that does
# not exist, so the two cases cannot be told apart from the response body.
NOT_FOUND_MESSAGE = "No Document matches the given query."

ACTION_TO_AUDIT_EVENT = {
    "retrieve": AuditLog.Action.DOCUMENT_VIEW,
    "update": AuditLog.Action.DOCUMENT_UPDATE,
    "partial_update": AuditLog.Action.DOCUMENT_UPDATE,
    "destroy": AuditLog.Action.DOCUMENT_DELETE,
    "reprocess": AuditLog.Action.DOCUMENT_REPROCESS,
    "download": AuditLog.Action.DOCUMENT_DOWNLOAD,
    # Extracted text is the document's substance - reading it is a view.
    "content": AuditLog.Action.DOCUMENT_VIEW,
    # So is everything derived from it: a field value quotes the document,
    # and a chunk is a verbatim slice of it. Auditing the derived views
    # differently would make the cheapest way to read a document the one
    # that leaves the weakest trail.
    "classification": AuditLog.Action.DOCUMENT_VIEW,
    "extraction": AuditLog.Action.DOCUMENT_VIEW,
    "chunks": AuditLog.Action.DOCUMENT_VIEW,
    "tables": AuditLog.Action.DOCUMENT_VIEW,
    "figures": AuditLog.Action.DOCUMENT_VIEW,
    "source": AuditLog.Action.DOCUMENT_VIEW,
    "archive": AuditLog.Action.DOCUMENT_ARCHIVE,
    "restore": AuditLog.Action.DOCUMENT_RESTORE,
}


class DocumentAccessPolicy(permissions.BasePermission):
    """Object-level access control for documents, with audit logging.

    Read: document owner, someone in the document's department, or a
    system admin. Manage (update/delete/reprocess/new version/archive):
    owner, a department admin of that department, or a system admin.

    Two kinds of refusal, deliberately distinct:

    * Someone who may READ the document but not manage it gets a 403. They
      can see it exists, so saying "forbidden" discloses nothing.
    * Someone who may not read it at all gets a 404 identical to the one an
      unused id returns. A 403 there confirmed that the id belongs to a
      real document in another department, which turns sequential ids into
      an enumeration of every document in the institution - titles aside,
      that is already a disclosure of how much each department holds.

    Both are still recorded as DENIED in the audit log. The response hides
    the document from the caller; it must not hide the attempt from an
    auditor.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, document):
        is_read = view.action in READ_ONLY_ACTIONS or (
            view.action == "versions" and request.method in permissions.SAFE_METHODS
        )

        can_view = authorization.can_view_document(request.user, document)
        allowed = can_view and (
            is_read or authorization.can_manage_document(request.user, document)
        )

        action = self._audit_action(view, request)
        if action is None and not allowed:
            # A refusal is recorded whatever the action. Unmapped actions
            # (the processing timeline, polled every few seconds and holding
            # no document content) are not audited when ALLOWED, but an
            # outsider's attempt on one must not vanish from the trail.
            action = AuditLog.Action.DOCUMENT_VIEW
        if action:
            metadata = {}
            # Which version was fetched is part of what happened. Only a
            # well-formed number is recorded - the raw parameter is caller
            # input and has no business in the audit trail unvalidated.
            requested = request.query_params.get("version", "")
            if view.action == "download" and requested.isdigit():
                metadata["version_number"] = int(requested[:9])
            record_event(
                user=request.user,
                action=action,
                document=document,
                result=AuditLog.Result.ALLOWED if allowed else AuditLog.Result.DENIED,
                **metadata,
            )
            if not allowed:
                telemetry.authorization_denied(
                    action, user_id=request.user.id, document_id=document.id
                )

        if not can_view:
            raise Http404(NOT_FOUND_MESSAGE)

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
