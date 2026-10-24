from django.conf import settings
from django.contrib.auth.signals import user_login_failed
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_profile(sender, instance, created, **kwargs):
    if not created:
        return

    UserProfile.objects.create(
        user=instance,
        role=(
            UserProfile.Role.SYSTEM_ADMIN
            if instance.is_superuser
            else UserProfile.Role.STUDENT
        ),
    )


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def forget_applicants_passwords(sender, instance, created, **kwargs):
    """Once an account holds a name, the sign-up requests for it lose
    their password hashes, however the account was made.

    A declined request keeps its hash so that sign-in can tell the
    applicant why they cannot get in - but that stops the moment an account
    takes the name (accounts.services.unapproved_signup never looks past
    it), and the hash, often of a password the person uses elsewhere, would
    otherwise outlive every reason to keep it: the approval of a later
    request, an administrator creating the account by hand, that person's
    own password changes.
    """
    if not created:
        return

    from .models import SignupRequest
    from .services import same_name

    same_name(
        SignupRequest.objects.exclude(password=""), instance.get_username()
    ).update(password="")


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def revoke_api_tokens_on_password_change(sender, instance, created, **kwargs):
    """A new password ends every API token, however it was set.

    The API's own password endpoints revoke tokens themselves, but Django's
    admin password form, `manage.py changepassword` and a shell
    `set_password()` did not - after a reset meant to lock someone out,
    their tokens kept working until they expired. Sessions need nothing: a
    session dies with the old password hash.

    `_password` is how Django itself tells a change from a hash upgrade: it
    is set by set_password() and is still set while post_save runs, while
    the upgrade on login clears it before saving ("hash upgrades shouldn't
    be considered password changes").
    """
    if created or getattr(instance, "_password", None) is None:
        return

    from .services import revoke_api_tokens

    revoke_api_tokens(instance)


@receiver(user_login_failed)
def audit_failed_login_outside_the_api(sender, credentials, request=None, **kwargs):
    """Failed password checks that do not come through the API's own views.

    Django's admin login authenticates by itself, so its failures wrote no
    audit row and were never counted - on the one login page that accepts
    superusers. The API's views record their own failures (with their
    method), so they are skipped here rather than recorded twice.
    """
    path = getattr(request, "path", "") or ""
    if request is None or path.startswith("/api/"):
        return

    from .services import record_failed_login

    record_failed_login(str(credentials.get("username", "")), method="django-admin")


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def end_access_on_deactivation(sender, instance, created, update_fields=None, **kwargs):
    """A deactivated account keeps no session and no token, however it was
    deactivated - the API, Django's admin or a shell."""
    if created or instance.is_active:
        return
    if update_fields is not None and "is_active" not in update_fields:
        return

    from .services import end_sessions, revoke_api_tokens

    revoke_api_tokens(instance)
    end_sessions(instance)
