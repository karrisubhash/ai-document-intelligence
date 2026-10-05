"""API token authentication with a lifetime.

DRF's TokenAuthentication issues a token that is valid until someone deletes
it - in practice, forever. A token copied into a script, a CI variable or a
browser's localStorage in the first week of a deployment is still a full
credential three years later, long after the person it belonged to has
changed role. So tokens here expire, and an expired one is deleted on first
sight rather than left in the table to be tried again.

Browsers do not use tokens at all any more; they hold an httpOnly session
cookie (see accounts.api.views.LoginView). Tokens remain for scripts and
service clients, which is the use they are fit for.
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token

DEFAULT_TOKEN_TTL_HOURS = 12


def token_ttl() -> timedelta:
    return timedelta(
        hours=getattr(settings, "TOKEN_TTL_HOURS", DEFAULT_TOKEN_TTL_HOURS)
    )


def is_expired(token: Token) -> bool:
    return token.created < timezone.now() - token_ttl()


def issue_token(user) -> Token:
    """A token for `user`, replacing one that has expired.

    `Token.objects.get_or_create`, which DRF's own login view uses, hands back
    the existing row however old it is - so without this, logging in again
    would return the expired token that had just been refused.
    """
    token = Token.objects.filter(user=user).first()
    # Replaced when it has less than half its life left, not only once it
    # has expired: a client re-authenticating because it "must re-issue"
    # was otherwise handed back a token that might expire in seconds.
    if token is not None and token.created < timezone.now() - token_ttl() / 2:
        token.delete()
        token = None
    if token is None:
        try:
            with transaction.atomic():
                token = Token.objects.create(user=user)
        except IntegrityError:
            # Two simultaneous sign-ins both found no token; Token.user is
            # unique, so the second create lost the race. Theirs is as good.
            token = Token.objects.get(user=user)
    return token


class ExpiringTokenAuthentication(TokenAuthentication):
    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if is_expired(token):
            token.delete()
            raise exceptions.AuthenticationFailed("Token has expired.")
        return user, token
