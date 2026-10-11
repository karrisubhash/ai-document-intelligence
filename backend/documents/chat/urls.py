from django.urls import path

from .views import (
    ChatStreamView,
    ChatView,
    ConversationDetailView,
    ConversationListView,
)

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("stream/", ChatStreamView.as_view(), name="chat-stream"),
    path(
        "conversations/",
        ConversationListView.as_view(),
        name="conversation-list",
    ),
    path(
        "conversations/<int:pk>/",
        ConversationDetailView.as_view(),
        name="conversation-detail",
    ),
]
