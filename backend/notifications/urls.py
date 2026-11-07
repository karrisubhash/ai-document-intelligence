from django.urls import path

from .views import (
    NotificationListView,
    NotificationReadAllView,
    NotificationReadView,
    NotificationSummaryView,
)

urlpatterns = [
    path("", NotificationListView.as_view(), name="notification-list"),
    path("summary/", NotificationSummaryView.as_view(), name="notification-summary"),
    path("read-all/", NotificationReadAllView.as_view(), name="notification-read-all"),
    path("<int:pk>/read/", NotificationReadView.as_view(), name="notification-read"),
]
