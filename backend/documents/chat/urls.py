from django.urls import path

from .views import (
    ChatCapabilitiesView,
    ChatStreamView,
    ChatView,
    ConversationBulkDeleteView,
    ConversationDetailView,
    ConversationListView,
)

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("stream/", ChatStreamView.as_view(), name="chat-stream"),
    path(
        "capabilities/",
        ChatCapabilitiesView.as_view(),
        name="chat-capabilities",
    ),
    path(
        "conversations/",
        ConversationListView.as_view(),
        name="conversation-list",
    ),
    path(
        "conversations/bulk-delete/",
        ConversationBulkDeleteView.as_view(),
        name="conversation-bulk-delete",
    ),
    path(
        "conversations/<int:pk>/",
        ConversationDetailView.as_view(),
        name="conversation-detail",
    ),
]
