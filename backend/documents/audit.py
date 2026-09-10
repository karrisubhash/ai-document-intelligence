from .models import AuditLog


def record_event(*, user, action, result, document=None, **metadata):
    AuditLog.objects.create(
        user=user if user and user.is_authenticated else None,
        action=action,
        document=document,
        result=result,
        metadata=metadata,
    )
