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
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.db.models import Q, Value
from django.db.models.functions import Lower
from django.utils import timezone
from rest_framework.authtoken.models import Token

from .models import Department, SignupRequest, UserProfile


class UnknownDepartment(ValueError):
    pass


class DepartmentNameInUse(ValueError):
    pass


class SignupAlreadyReviewed(ValueError):
    pass


class UsernameTaken(ValueError):
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

    The name is the value profiles, documents, collections, conversations and
    evaluation datasets store, so a rename that changed only the registry row
    would silently move every member and document into a department that no
    longer exists - and out of reach of each other.
    """
    from documents.models import (
        AuditLog,
        Collection,
        Conversation,
        Document,
        EvaluationDataset,
    )

    old_name = department.name
    new_name = new_name.strip()
    if new_name == old_name:
        return department

    # Records already stored under the NEW spelling belong to a separate,
    # unregistered tenant (migration 0003 deliberately left near-duplicates
    # like "physics" beside "Physics" apart). Renaming onto that spelling
    # would merge the two and silently widen access between them - the
    # decision the migration left to an administrator, made by a typo.
    # The audit trail of a deleted document keeps its department too, and
    # is rewritten below, so a spelling that survives only there would
    # merge that trail into the renamed department's view.
    in_use = (
        any(
            model.objects.filter(department=new_name).exists()
            for model in (
                UserProfile,
                Document,
                Collection,
                Conversation,
                EvaluationDataset,
                SignupRequest,
            )
        )
        or AuditLog.objects.filter(document_department=new_name).exists()
    )
    if in_use:
        raise DepartmentNameInUse(
            f"Records already use the department name {new_name!r}; renaming "
            f"{old_name!r} onto it would merge two departments. Reassign "
            f"those records first if they belong together."
        )

    department.name = new_name
    department.save(update_fields=["name", "updated_at"])

    UserProfile.objects.filter(department=old_name).update(department=new_name)
    Document.objects.filter(department=old_name).update(department=new_name)
    # Left behind, a collection would vanish from its members, refuse every
    # document (they now carry the new name) and pass to whichever
    # department is next given the old name.
    Collection.objects.filter(department=old_name).update(department=new_name)
    Conversation.objects.filter(department=old_name).update(department=new_name)
    EvaluationDataset.objects.filter(department=old_name).update(department=new_name)
    # Or a waiting applicant would drop out of the department admin's queue,
    # and approving them as asked would name a department that is gone.
    SignupRequest.objects.filter(department=old_name).update(department=new_name)
    # Or a deleted document's trail would fall out of its department's view.
    AuditLog.objects.filter(document_department=old_name).update(
        document_department=new_name
    )

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


def same_name(queryset, username: str):
    """The rows of `queryset` whose username differs from `username` by
    case alone.

    Under both of PostgreSQL's case rules, because outside ASCII they
    disagree: iexact compares UPPER(), which folds the long s into "s" but
    leaves the Kelvin sign apart from "K"; LOWER(), which the waiting-request
    constraint uses, does the reverse. A name equal under either is the same
    name to a person reading a listing.
    """
    return queryset.alias(folded_username=Lower("username")).filter(
        Q(username__iexact=username) | Q(folded_username=Lower(Value(username)))
    )


def username_unavailable(username: str) -> bool:
    """Whether a sign-up may not ask for `username`.

    Case-insensitive, though Django's usernames are not: an applicant who
    became "Alice" beside an existing "alice" would be one typo away from
    someone else's sign-in, and an administrator could not tell them apart
    in a listing. A waiting request holds its name too, so two applicants
    cannot both be promised it.
    """
    return (
        same_name(get_user_model().objects.all(), username).exists()
        or same_name(
            SignupRequest.objects.filter(status=SignupRequest.Status.PENDING),
            username,
        ).exists()
    )


def _lock_pending(signup: SignupRequest) -> SignupRequest:
    # Locked, so two administrators deciding at once cannot both win - one
    # approving while the other rejects, or two accounts from one request.
    signup = SignupRequest.objects.select_for_update().get(pk=signup.pk)
    if signup.status != SignupRequest.Status.PENDING:
        raise SignupAlreadyReviewed(
            f"This request was already {signup.get_status_display().lower()}."
        )
    return signup


@transaction.atomic
def approve_signup(
    signup: SignupRequest, *, reviewer, role: str, department: str
) -> SignupRequest:
    """Turn a request into an account with the role and department given.

    Whether `reviewer` may grant that role and department is the caller's
    question (AdminUserSerializer's rules, applied by the review
    serializer); this does what approval means once it is allowed.
    """
    signup = _lock_pending(signup)
    User = get_user_model()
    if same_name(User.objects.all(), signup.username).exists():
        raise UsernameTaken(
            f"An account named {signup.username!r} was created after this "
            f"request was made. Decline the request and ask them to choose "
            f"another username."
        )

    user = User(
        username=signup.username,
        email=signup.email,
        first_name=signup.first_name,
        last_name=signup.last_name,
    )
    # The hash itself, so they sign in with the password they chose and
    # nobody - the administrator included - ever handles it.
    user.password = signup.password
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:
        # The same name created between the check above and this insert.
        raise UsernameTaken(
            f"An account named {signup.username!r} already exists."
        ) from exc

    # The post_save signal created the profile with defaults.
    profile = user.profile
    profile.role = role
    profile.department = department
    profile.save()

    signup.status = SignupRequest.Status.APPROVED
    signup.user = user
    signup.reviewed_by = reviewer
    signup.reviewed_at = timezone.now()
    signup.password = ""
    signup.save()
    return signup


@transaction.atomic
def reject_signup(signup: SignupRequest, *, reviewer, note: str = "") -> SignupRequest:
    signup = _lock_pending(signup)
    signup.status = SignupRequest.Status.REJECTED
    signup.decision_note = note
    signup.reviewed_by = reviewer
    signup.reviewed_at = timezone.now()
    signup.save()
    return signup


def unapproved_signup(username: str, password: str) -> SignupRequest | None:
    """The applicant's own waiting or declined request, if these are its
    credentials - for a sign-in that failed because there is no account yet.

    Only with the right password, so the sign-in form cannot be used to
    learn who has applied. Never when an account holds the name: that
    person's failed sign-in is theirs, whatever an earlier applicant for
    the same name typed. The name must match exactly, as it will have to
    once the account exists: "Nia" told "waiting" for a request filed as
    "nia" would be told "wrong password" the day it was approved.

    Every path costs exactly one password hash. The failed authenticate()
    before this has spent one already, whoever the name belongs to; a
    second only when a request holds the name would let the TIME a failed
    sign-in takes say who has applied, password or not.
    """
    signup = None
    if not same_name(get_user_model().objects.all(), username).exists():
        signup = (
            SignupRequest.objects.filter(username=username)
            .exclude(status=SignupRequest.Status.APPROVED)
            .exclude(password="")
            # The newest: a declined applicant who applied again is waiting.
            .order_by("-created_at", "-id")
            .first()
        )
    if signup is None:
        # The same work as the check below, done for nothing.
        make_password(password)
        return None
    return signup if check_password(password, signup.password) else None


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
