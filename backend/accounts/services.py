"""Identity operations that touch more than one table.

Kept out of the views so the rules - which department names are valid, what
a rename has to rewrite, what deactivating an account has to revoke - exist
in one place and are the same whether they are reached through the API, the
Django admin or a management command.
"""

import hashlib
import hmac

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.authtoken.models import Token

from .models import Department, UserProfile


class UnknownDepartment(ValueError):
    pass


class DepartmentNameInUse(ValueError):
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

    # Records already stored under the NEW spelling belong to a separate,
    # unregistered tenant (migration 0003 deliberately left near-duplicates
    # like "physics" beside "Physics" apart). Renaming onto that spelling
    # would merge the two and silently widen access between them - the
    # decision the migration left to an administrator, made by a typo.
    for model in (UserProfile, Document, Conversation, EvaluationDataset):
        if model.objects.filter(department=new_name).exists():
            raise DepartmentNameInUse(
                f"Records already use the department name {new_name!r}; renaming "
                f"{old_name!r} onto it would merge two departments. Reassign "
                f"those records first if they belong together."
            )

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


def failed_login_fingerprint(username: str) -> str:
    """A keyed fingerprint of an attempted username that matches no account.

    The name itself is not kept. The username box is where people type their
    password by mistake, so a cleartext "attempted username" column slowly
    collects passwords for anyone who can read the audit log. Keyed with the
    secret key so the stored value cannot be reversed by guessing; stable,
    so a run of attempts against one name still shows as one name.
    """
    normalised = username.strip().lower().encode()
    return hmac.new(
        settings.SECRET_KEY.encode(), normalised, hashlib.sha256
    ).hexdigest()[:16]


def record_failed_login(username: str, *, method: str) -> None:
    """Audit and count one failed password check, whichever door it came in.

    The attempt belongs on the targeted account's trail; for a name that
    matches no account, only its fingerprint is kept.
    """
    from documents.models import AuditLog
    from documents.observability import telemetry

    target = get_user_model().objects.filter(username=username).first()
    metadata = {"method": method}
    if target is None:
        metadata["attempted_username_fingerprint"] = failed_login_fingerprint(username)
    AuditLog.objects.create(
        user=target,
        action=AuditLog.Action.LOGIN_FAILED,
        result=AuditLog.Result.DENIED,
        metadata=metadata,
    )
    telemetry.login_failed(method)


def end_sessions(user) -> int:
    """Delete every live session belonging to `user`.

    For deactivation. An inactive account's session is REFUSED, not ended:
    Django returns AnonymousUser for it and leaves the session in place, so
    reactivating the account within the session's lifetime revived every
    browser that had been signed in when it was switched off. Sessions are
    found by decoding them - O(live sessions), acceptable for an
    administrative action - and deleted through the configured engine, so
    the cache copy of a cached_db session goes too.
    """
    from importlib import import_module

    from django.contrib.sessions.models import Session
    from django.utils import timezone

    store = import_module(settings.SESSION_ENGINE).SessionStore
    ended = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if session.get_decoded().get("_auth_user_id") == str(user.pk):
            store(session_key=session.session_key).delete()
            ended += 1
    return ended
