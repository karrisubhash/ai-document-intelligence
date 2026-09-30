from django.db import connection
from django.http import JsonResponse


def health_check(request):
    """Liveness: the process is up and can reach its database.

    Kept deliberately narrow. A liveness probe that also failed on Redis
    would have the orchestrator RESTART every API container whenever Redis
    blinked - replacing a degraded service with no service at all.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

        return JsonResponse(
            {
                "status": "healthy",
                "database": "healthy",
            }
        )

    except Exception:
        return JsonResponse(
            {
                "status": "unhealthy",
                "database": "unhealthy",
            },
            status=503,
        )


def readiness_check(request):
    """Readiness: every dependency a request needs is reachable.

    The probe for a load balancer deciding whether to SEND traffic here,
    which is a different question from whether to restart the process. A
    503 takes this instance out of rotation until Redis or the broker comes
    back, rather than serving requests that would fail anyway.

    Statuses and latencies only - never a host, a port or an error message.
    """
    from documents.operations import health

    ok, results = health.ready()
    return JsonResponse(
        {"status": "ready" if ok else "not_ready", "components": results},
        status=200 if ok else 503,
    )
