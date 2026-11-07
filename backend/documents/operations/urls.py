from django.urls import path

from .analytics import AnalyticsView
from .feedback import FeedbackListView
from .views import AuditLogView, JobListView, JobRetryView, SystemStatusView

urlpatterns = [
    path("audit/", AuditLogView.as_view(), name="admin-audit"),
    path("jobs/", JobListView.as_view(), name="admin-jobs"),
    path("jobs/<int:pk>/retry/", JobRetryView.as_view(), name="admin-job-retry"),
    path("system/", SystemStatusView.as_view(), name="admin-system"),
    path("analytics/", AnalyticsView.as_view(), name="admin-analytics"),
    path("feedback/", FeedbackListView.as_view(), name="admin-feedback"),
]
