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
    return (
        bool(user.is_superuser)
        or get_profile(user).role == UserProfile.Role.SYSTEM_ADMIN
    )


def shares_department(profile, document) -> bool:
    """Whether a profile's department grants access to a document.

    An unset department is NOT a tenant. Both `UserProfile.department` and
    `Document.department` default to "", so a plain equality test would make
    every user who has never been assigned a department a colleague of every
    other one - and the default role is STUDENT, so that is the state a
    freshly registered user is in. Empty matches nothing but itself being
    absent, which is the only safe reading.
    """
    return bool(profile.department) and document.department == profile.department


def can_view_document(user, document) -> bool:
    if is_system_admin(user):
        return True

    if document.owner_id == user.id:
        return True

    return shares_department(get_profile(user), document)


def can_manage_document(user, document) -> bool:
    """Manage = update, delete, reprocess, or add a new version."""
    if is_system_admin(user):
        return True

    if document.owner_id == user.id:
        return True

    profile = get_profile(user)
    return profile.role == UserProfile.Role.DEPARTMENT_ADMIN and shares_department(
        profile, document
    )


def visible_documents(user, queryset):
    """Scope a queryset to what `user` is allowed to see (for listing).

    Mirrors can_view_document exactly, including its treatment of an unset
    department - a listing that showed more than `retrieve` would allow is
    itself the leak.
    """
    if is_system_admin(user):
        return queryset

    profile = get_profile(user)
    visible = Q(owner=user)
    if profile.department:
        visible |= Q(department=profile.department)

    return queryset.filter(visible)


# --- Collections (spec 14) ------------------------------------------------
#
# The roles that may curate collections: the spec's ORG_ADMIN and MANAGER
# (DEPARTMENT_ADMIN and FACULTY here) and the platform administrator.
COLLECTION_CURATORS = frozenset(
    {
        UserProfile.Role.SYSTEM_ADMIN,
        UserProfile.Role.DEPARTMENT_ADMIN,
        UserProfile.Role.FACULTY,
    }
)


def can_create_collection(user) -> bool:
    if is_system_admin(user):
        return True
    profile = get_profile(user)
    # A department-less curator would create a collection nobody else could
    # see, holding documents nobody could add - refused, not silently empty.
    return profile.role in COLLECTION_CURATORS and bool(profile.department)


def visible_collections(user, queryset):
    """Same shape as visible_documents: own department, or created by `user`."""
    if is_system_admin(user):
        return queryset
    profile = get_profile(user)
    visible = Q(created_by=user)
    if profile.department:
        visible |= Q(department=profile.department)
    return queryset.filter(visible)


def can_manage_collection(user, collection) -> bool:
    """Rename, delete, or change membership."""
    if is_system_admin(user):
        return True
    if collection.created_by_id == user.id:
        return True
    profile = get_profile(user)
    return (
        profile.role == UserProfile.Role.DEPARTMENT_ADMIN
        and bool(profile.department)
        and collection.department == profile.department
    )
