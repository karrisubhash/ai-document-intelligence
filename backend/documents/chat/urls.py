from django.urls import path

from .views import ChatView, ConversationDetailView

urlpatterns = [
    path("", ChatView.as_view(), name="chat"),
    path("conversations/<int:pk>/", ConversationDetailView.as_view(), name="conversation-detail"),
]
