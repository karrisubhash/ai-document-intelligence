from django.contrib import admin
from django.urls import include, path
from rest_framework.authtoken.views import ObtainAuthToken

from config.health import health_check
from documents.observability.views import metrics_view
from documents.evaluation_api.views import FeedbackView
from documents.throttling import LoginRateThrottle


class ThrottledObtainAuthToken(ObtainAuthToken):
    """The login endpoint is the one place an unauthenticated caller can
    make the server do real work with attacker-chosen input."""

    throttle_classes = [LoginRateThrottle]


obtain_auth_token = ThrottledObtainAuthToken.as_view()


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/documents/", include("documents.api.urls")),
    path("api/search/", include("documents.search.urls")),
    path("api/chat/", include("documents.chat.urls")),
    path("api/evaluations/", include("documents.evaluation_api.urls")),
    path(
        "api/messages/<int:message_id>/feedback/",
        FeedbackView.as_view(),
        name="message-feedback",
    ),
    path("api/auth/token/", obtain_auth_token, name="api-token-auth"),
    path("api/auth/", include("accounts.api.urls")),
    path("health/", health_check),
    path("metrics", metrics_view, name="metrics"),
]

# MEDIA_ROOT is deliberately NOT served here. Django's static() helper
# serves files with no authentication at all, which would hand the raw
# document bytes to anonymous callers and bypass every access decision
# in documents.authorization. Files are only reachable through the
# authenticated, audited /api/documents/{id}/download/ endpoint.