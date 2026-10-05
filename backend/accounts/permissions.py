"""Who may administer whom.

Plain functions, like documents.authorization, so the API, the admin screens
and any future command all ask the same question the same way.

Two administrative tiers, and the line between them is the tenant boundary:

* A SYSTEM_ADMIN administers every account and every department.
* A DEPARTMENT_ADMIN administers the ordinary accounts of their own
  department - FACULTY, STAFF and STUDENT - and nothing else. They cannot
  see or edit another department's people, cannot touch another admin, and
  cannot mint one: a department admin who could grant DEPARTMENT_ADMIN or
  SYSTEM_ADMIN could escalate themselves through a second account in one
  request.

An admin can never change their own role or deactivate their own account.
That is not paternalism - it is what stops the last system administrator
locking everyone out with one mis-click, which is otherwise recoverable only
from a database shell.
"""

from rest_framework.permissions import BasePermission

from documents.authorization import get_profile, is_system_admin

from .models import UserProfile

Role = UserProfile.Role

# The roles a department admin may hand out. Deliberately excludes both
# administrative roles.
DEPARTMENT_ASSIGNABLE_ROLES = frozenset({Role.FACULTY, Role.STAFF, Role.STUDENT})


def is_department_admin(user) -> bool:
    profile = get_profile(user)
    # An unset department is not a tenant, so it cannot be administered.
    return profile.role == Role.DEPARTMENT_ADMIN and bool(profile.department)


def is_administrator(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and (is_system_admin(user) or is_department_admin(user))
    )


def administrable_users(actor, queryset):
    if is_system_admin(actor):
        return queryset
    if is_department_admin(actor):
        return queryset.filter(profile__department=get_profile(actor).department)
    return queryset.none()


def assignable_roles(actor) -> frozenset:
    if is_system_admin(actor):
        return frozenset(Role.values)
    if is_department_admin(actor):
        return DEPARTMENT_ASSIGNABLE_ROLES
    return frozenset()


def can_administer(actor, target) -> bool:
    """Whether `actor` may edit `target`'s account at all.

    Nobody may take over an account holding more power than they have. The
    API never GRANTS is_superuser or is_staff, but resetting the password of
    an account that already has them grants the same thing: a role-only
    system administrator could set a superuser's password and sign in to
    Django's admin as one, and a department administrator could do the same
    with a staff account carrying Django model permissions.
    """
    if target.is_superuser and not actor.is_superuser:
        return False
    if is_system_admin(actor):
        return True
    if not is_department_admin(actor):
        return False
    if target.is_staff:
        return False
    profile = get_profile(target)
    return (
        profile.department == get_profile(actor).department
        and profile.role in DEPARTMENT_ASSIGNABLE_ROLES
    )


class IsAdministrator(BasePermission):
    """System or department administrator."""

    def has_permission(self, request, view):
        return is_administrator(request.user)


class IsSystemAdministrator(BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and is_system_admin(request.user)
        )
