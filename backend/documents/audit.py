from .models import AuditLog


def record_event(
    *, user, action, result, document=None, subject_department="", **metadata
):
    """Append one audit row.

    `subject_department` is for a subject that is not a document but belongs
    to a department - a collection, a sign-up request - so that department's
    administrator sees the row whoever acted (the action must be one of
    operations.views.SUBJECT_DEPARTMENT_ACTIONS). A document's department is
    taken from the document.
    """
    AuditLog.objects.create(
        user=user if user and user.is_authenticated else None,
        action=action,
        document=document,
        document_department=subject_department,
        result=result,
        metadata=metadata,
    )
