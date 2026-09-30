"""Per-request correlation id, structured access log and API metrics."""

import logging
import re
import time

from . import metrics
from .context import (
    HEADER,
    RESPONSE_HEADER,
    reset_request_id,
    set_request_id,
)
from .logging import log_event

logger = logging.getLogger("adi.request")

_NAMED_GROUP = re.compile(r"\(\?P<(\w+)>[^)]*\)")

# Routes that would otherwise dominate the metrics with noise. /metrics is
# scraped every few seconds and /health/ by every orchestrator probe;
# counting them buries the traffic anyone actually cares about.
UNMEASURED = ("/metrics", "/health/")


def _route(request) -> str:
    """The URL PATTERN, not the resolved path.

    /api/documents/7/ and /api/documents/8/ must be one time series. Using
    the raw path would mint a new series per document and eventually take
    the metrics backend down - a self-inflicted outage caused by the
    instrumentation rather than the system.
    """
    match = getattr(request, "resolver_match", None)
    if match is None or not match.route:
        return "unmatched"

    # DRF's router contributes regex fragments - "api/documents/$" and
    # "api/documents/(?P<pk>[^/.]+)/$". Normalised to a readable, still
    # BOUNDED label: one series per pattern, never per document.
    route = match.route.strip("^$")
    route = _NAMED_GROUP.sub(lambda match: "{" + match.group(1) + "}", route)
    return "/" + route.lstrip("/")


def _infrastructure_views():
    """The endpoints served ahead of the rest of the middleware stack.

    /metrics, /health/ and /ready/ exist for callers INSIDE the deployment:
    Prometheus scrapes each backend replica by its container IP, and
    container probes call localhost. Neither sends the public hostname, and
    both speak plain HTTP. Routed through the full stack, every such call
    failed before reaching its view - CommonMiddleware rejects an unknown
    Host with DisallowedHost (400), and SecurityMiddleware redirects plain
    HTTP to https. Measured on the production stack: every Prometheus scrape
    for the lifetime of the deployment returned 400, so the dashboards were
    empty.

    Served here, before either check, because none of the three builds a
    URL, reads a session or returns user content - host validation protects
    nothing on them. /metrics keeps its own address allowlist, and nginx
    still refuses it from outside private ranges.
    """
    from config.health import health_check, readiness_check

    from .views import metrics_view

    return {
        "/metrics": metrics_view,
        "/health/": health_check,
        "/ready/": readiness_check,
    }


class ObservabilityMiddleware:
    """Assigns a request id, logs the request, and records API metrics."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._infrastructure = None

    def __call__(self, request):
        if self._infrastructure is None:
            self._infrastructure = _infrastructure_views()
        view = self._infrastructure.get(request.path)
        if view is not None and request.method in ("GET", "HEAD"):
            return view(request)

        request_id = set_request_id(request.META.get(HEADER, ""))
        started = time.monotonic()

        try:
            response = self.get_response(request)
        except Exception:
            # Metrics for an unhandled exception are recorded here because
            # the response never comes back to the code below.
            duration = time.monotonic() - started
            if not self._skip(request):
                metrics.http_requests.labels(
                    method=request.method, route=_route(request), status="500"
                ).inc()
                metrics.http_duration.labels(
                    method=request.method, route=_route(request)
                ).observe(duration)
            log_event(
                logger,
                logging.ERROR,
                "http.request.failed",
                method=request.method,
                route=_route(request),
                duration_ms=round(duration * 1000, 2),
            )
            reset_request_id()
            raise

        duration = time.monotonic() - started
        response[RESPONSE_HEADER] = request_id

        if not self._skip(request):
            route = _route(request)
            status = str(response.status_code)

            metrics.http_requests.labels(
                method=request.method, route=route, status=status
            ).inc()
            metrics.http_duration.labels(method=request.method, route=route).observe(
                duration
            )

            if response.status_code == 429:
                metrics.throttled_requests.labels(route=route).inc()
            if response.status_code == 401:
                metrics.auth_failures.inc()

            # The path is deliberately NOT logged: a URL can carry a
            # document id, which is fine, but it can also carry a filename
            # or a search term depending on how a client builds it. The
            # route pattern says what was called without that risk.
            log_event(
                logger,
                logging.INFO,
                "http.request",
                method=request.method,
                route=route,
                status=response.status_code,
                duration_ms=round(duration * 1000, 2),
                user_id=getattr(getattr(request, "user", None), "id", None),
            )

        reset_request_id()
        return response

    @staticmethod
    def _skip(request) -> bool:
        return any(request.path.startswith(prefix) for prefix in UNMEASURED)
