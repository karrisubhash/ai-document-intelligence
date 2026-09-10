from django.db import connection
from django.http import JsonResponse


def health_check(request):
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
