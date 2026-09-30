"""Identity operations that touch more than one table.

Kept out of the views so the rules - which department names are valid, what
a rename has to rewrite, what deactivating an account has to revoke - exist
in one place and are the same whether they are reached through the API, the
Django admin or a management command.
"""

from django.db import transaction
from rest_framework.authtoken.models import Token

from .models import Department, UserProfile


class UnknownDepartment(ValueError):
    pass


def resolve_department(name: str, *, allow_inactive: bool = False) -> str:
    """The registry's spelling of a department, or "" for none.

    Raises UnknownDepartment for a name the registry does not hold. Matching
    is case-insensitive and ignores surrounding whitespace, because those are
    exactly the variations that used to create accidental tenants; what is
    stored is always the registry's own spelling.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return ""

    department = Department.objects.filter(name__iexact=cleaned).first()
    if department is None:
        raise UnknownDepartment(f"{cleaned!r} is not a registered department.")
    if not department.is_active and not allow_inactive:
        raise UnknownDepartment(
            f"{department.name!r} is inactive and accepts no new assignments."
        )
    return department.name


@transaction.atomic
def rename_department(department: Department, new_name: str) -> Department:
    """Rename a department and every reference to it, atomically.

    The name is the value profiles, documents, conversations and evaluation
    datasets store, so a rename that changed only the registry row would
    silently move every member and document into a department that no
    longer exists - and out of reach of each other.
    """
    from documents.models import Conversation, Document, EvaluationDataset

    old_name = department.name
    new_name = new_name.strip()
    if new_name == old_name:
        return department

    department.name = new_name
    department.save(update_fields=["name", "updated_at"])

    UserProfile.objects.filter(department=old_name).update(department=new_name)
    Document.objects.filter(department=old_name).update(department=new_name)
    Conversation.objects.filter(department=old_name).update(department=new_name)
    EvaluationDataset.objects.filter(department=old_name).update(department=new_name)

    return department


def revoke_api_tokens(user) -> int:
    """Delete every API token the user holds.

    Called whenever the answer to "may this person still act?" changes -
    deactivation, a password change, logout. Sessions need no equivalent:
    Django invalidates a session when the password hash changes, and refuses
    one for an inactive account.
    """
    deleted, _ = Token.objects.filter(user=user).delete()
    return deleted
