from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden

from . import metrics, queues


def metrics_view(request):
    """Prometheus scrape endpoint.

    Unauthenticated by default because a scraper is usually an infrastructure
    component without credentials, and gated by METRICS_ALLOWED_IPS so it is
    not open to the internet by accident. Nothing here carries document
    content - a counter can only hold a number - but request rates and error
    counts are still operational intelligence worth keeping internal.
    """
    allowed = getattr(settings, "METRICS_ALLOWED_IPS", [])
    if allowed:
        remote = request.META.get("REMOTE_ADDR", "")
        if remote not in allowed:
            return HttpResponseForbidden("metrics are not exposed to this address")

    # Refreshed per scrape rather than on a timer: the gauge is only
    # meaningful at the moment it is read, and a stale queue depth is
    # the metric most likely to be believed when it is wrong.
    queues.collect()

    payload, content_type = metrics.render()
    return HttpResponse(payload, content_type=content_type)
