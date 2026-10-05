from django.contrib import admin
from django.urls import include, path

from accounts.api.views import DepartmentListView
from config.health import health_check, readiness_check
from documents.evaluation_api.views import FeedbackView
from documents.observability.views import metrics_view
from documents.operations.views import DashboardView

urlpatterns = [
    # NOT "admin/": the application's own administration screens live at
    # /admin/users, /admin/audit, ... and the edge sends a path to Django or
    # to the app by prefix. With Django's admin mounted at /admin/, those
    # screens worked only through in-app navigation - a reload, a bookmark or
    # a new tab got Django's admin login instead. This is the operators'
    # back door for superusers; it does not need the obvious URL.
    path("django-admin/", admin.site.urls),
    path("api/documents/", include("documents.api.urls")),
    path("api/search/", include("documents.search.urls")),
    path("api/chat/", include("documents.chat.urls")),
    path("api/evaluations/", include("documents.evaluation_api.urls")),
    path(
        "api/messages/<int:message_id>/feedback/",
        FeedbackView.as_view(),
        name="message-feedback",
    ),
    # Login, logout, CSRF, password change and API tokens. The token
    # endpoint keeps its old path and name so existing scripts still work;
    # it now issues expiring tokens (accounts.authentication).
    path("api/auth/", include("accounts.api.urls")),
    path("api/departments/", DepartmentListView.as_view(), name="department-list"),
    path("api/dashboard/", DashboardView.as_view(), name="dashboard"),
    # Administration: accounts and departments (accounts app), and the
    # operational read models - audit, jobs, system status (documents app).
    path("api/admin/", include("accounts.api.admin_urls")),
    path("api/admin/", include("documents.operations.urls")),
    path("health/", health_check),
    path("ready/", readiness_check),
    path("metrics", metrics_view, name="metrics"),
]

# MEDIA_ROOT is deliberately NOT served here. Django's static() helper
# serves files with no authentication at all, which would hand the raw
# document bytes to anonymous callers and bypass every access decision
# in documents.authorization. Files are only reachable through the
# authenticated, audited /api/documents/{id}/download/ endpoint.
