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
