"""Configuration checks for the one-click demo (`manage.py check`).

DEMO_MAX_GUESTS is meant as a bound on what the demo adds to the database,
not a door that can be held shut. Starting a demo takes nothing but a CSRF
cookie, and the per-address limit is no obstacle to anyone with a few
addresses, so the only bound on how many guests can exist at once is the
demo-wide start rate (THROTTLE_DEMO_GLOBAL) over a guest's lifetime. These
warnings say when the configuration lets the ceiling be reached anyway.
Warnings, not errors: reaching it is a demo that says "full", not a breach,
and an operator may accept that.
"""

import math

from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured


def _starts_within(seconds: float) -> int | None:
    """The most demo starts THROTTLE_DEMO_GLOBAL lets through in any span
    of `seconds`, or None for a rate this cannot read (DRF says so itself
    when the rate is used)."""
    from .throttling import DemoGlobalRateThrottle

    try:
        throttle = DemoGlobalRateThrottle()
    except (ImproperlyConfigured, IndexError, KeyError, ValueError):
        return None
    if not throttle.num_requests or not throttle.duration:
        return None
    # The throttle admits num_requests in any window of `duration`, so a
    # span covering N windows admits N times as many.
    return throttle.num_requests * math.ceil(seconds / throttle.duration)


@checks.register()
def demo_capacity(app_configs, **kwargs):
    if not settings.DEMO_ENABLED:
        return []
    ceiling = settings.DEMO_MAX_GUESTS
    messages = []

    lifetime = _starts_within(settings.DEMO_GUEST_TTL_HOURS * 3600)
    if lifetime is not None and lifetime > ceiling:
        messages.append(
            checks.Warning(
                f"THROTTLE_DEMO_GLOBAL lets {lifetime} demos start within "
                f"DEMO_GUEST_TTL_HOURS ({settings.DEMO_GUEST_TTL_HOURS} h), more "
                f"than the DEMO_MAX_GUESTS ({ceiling}) guests that may exist at "
                f"once.",
                hint=(
                    "Visitors who keep their guests busy can then fill the demo, "
                    "and every new visitor is told it is full until one expires. "
                    "Lower DEMO_GUEST_TTL_HOURS or THROTTLE_DEMO_GLOBAL, or raise "
                    "DEMO_MAX_GUESTS (docs/deployment/production.md)."
                ),
                id="accounts.W001",
            )
        )

    idle = _starts_within(settings.DEMO_GUEST_IDLE_MINUTES * 60)
    if idle is not None and idle >= ceiling:
        messages.append(
            checks.Warning(
                f"THROTTLE_DEMO_GLOBAL lets {idle} demos start within "
                f"DEMO_GUEST_IDLE_MINUTES ({settings.DEMO_GUEST_IDLE_MINUTES} "
                f"min), as many as DEMO_MAX_GUESTS ({ceiling}).",
                hint=(
                    "A guest makes way for a new visitor only once idle that "
                    "long, so even guests nobody uses - a script starting demos "
                    "and walking away - could hold the demo full. Lower "
                    "DEMO_GUEST_IDLE_MINUTES or THROTTLE_DEMO_GLOBAL, or raise "
                    "DEMO_MAX_GUESTS."
                ),
                id="accounts.W002",
            )
        )
    return messages
