"""Document access decisions.

Plain functions rather than a DRF permission class, so the exact same
rules can be called directly by non-HTTP consumers later (document
search, vector retrieval, RAG) - the AI assistant must never surface a
document a user isn't authorized to see, and that can't be guaranteed
if the rule only exists inside a view.
"""

from django.db.models import Q

from accounts.models import UserProfile


def get_profile(user) -> UserProfile:
    profile = getattr(user, "profile", None)
    if profile is not None:
        return profile

    # Defensive fallback only - every real user gets a profile via the
    # post_save signal. Never let a missing row silently grant access.
    return UserProfile(user=user, role=UserProfile.Role.STUDENT, department="")


def is_system_admin(user) -> bool:
    return bool(user.is_superuser) or get_profile(user).role == UserProfile.Role.SYSTEM_ADMIN


def can_view_document(user, document) -> bool:
    if is_system_admin(user):
        return True

    profile = get_profile(user)
    return document.owner_id == user.id or document.department == profile.department


def can_manage_document(user, document) -> bool:
    """Manage = update, delete, reprocess, or add a new version."""
    if is_system_admin(user):
        return True

    if document.owner_id == user.id:
        return True

    profile = get_profile(user)
    return (
        profile.role == UserProfile.Role.DEPARTMENT_ADMIN
        and document.department == profile.department
    )


def visible_documents(user, queryset):
    """Scope a queryset to what `user` is allowed to see (for listing)."""
    if is_system_admin(user):
        return queryset

    profile = get_profile(user)
    return queryset.filter(Q(owner=user) | Q(department=profile.department))
