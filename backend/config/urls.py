from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

from config.health import health_check


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/documents/", include("documents.api.urls")),
    path("api/auth/token/", obtain_auth_token, name="api-token-auth"),
    path("api/auth/", include("accounts.api.urls")),
    path("health/", health_check),
]

# MEDIA_ROOT is deliberately NOT served here. Django's static() helper
# serves files with no authentication at all, which would hand the raw
# document bytes to anonymous callers and bypass every access decision
# in documents.authorization. Files are only reachable through the
# authenticated, audited /api/documents/{id}/download/ endpoint.