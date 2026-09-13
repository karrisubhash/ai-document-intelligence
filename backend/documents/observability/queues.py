"""Queue depth, read from the broker (spec §20, §22).

The `queue_depth` gauge was declared in phase 9 and never populated, which
made it worse than absent: a dashboard panel that is always zero reads as
"nothing is backing up" rather than "nobody is measuring". §22 says scaling
decisions are driven by measured queue depth, so this is the measurement
that makes "add another worker" an observation rather than a hunch.

Read from Redis rather than from Celery's inspect API on purpose. `inspect`
broadcasts to live workers and waits for replies: it reports what workers
are DOING, blocks for a timeout when none answer, and returns nothing at all
when every worker is dead - which is exactly the moment queue depth matters
most. A LLEN against the broker is one round trip and is correct whether or
not anything is consuming.
"""

import logging

from django.conf import settings

from . import metrics

logger = logging.getLogger("adi.queues")


def broker_url() -> str:
    return getattr(settings, "CELERY_BROKER_URL", "") or ""


def queue_names() -> list:
    """Every queue this deployment routes work to, plus the default.

    Taken from the routing table rather than hard-coded, so adding a route
    cannot silently create a queue nobody watches.
    """
    routes = getattr(settings, "CELERY_TASK_ROUTES", {}) or {}
    names = {
        route["queue"]
        for route in routes.values()
        if isinstance(route, dict) and route.get("queue")
    }
    names.add(getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "celery"))
    return sorted(names)


def collect() -> dict:
    """Set the gauge from the broker. Returns the depths it measured.

    Never raises: a metrics scrape must not fail because Redis blinked, and
    a scrape that 500s takes the dashboard down at the same moment the
    system is under stress.
    """
    depths = {}
    url = broker_url()

    if not url.startswith("redis"):
        # Another broker would need its own probe; saying so beats
        # reporting zeros that look like an idle system.
        logger.debug("queue.depth.unsupported_broker")
        return depths

    try:
        import redis

        client = redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)

        for name in queue_names():
            # Celery's Redis transport stores a queue as a plain list keyed
            # by the queue name.
            depth = client.llen(name)
            depths[name] = depth
            metrics.queue_depth.labels(queue=name).set(depth)

    except Exception as exc:  # pragma: no cover - depends on the broker
        logger.warning("queue.depth.failed", extra={"error": type(exc).__name__})

    return depths
