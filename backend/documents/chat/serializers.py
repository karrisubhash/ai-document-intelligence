from django.db.models import F
from django.db.models.manager import BaseManager
from rest_framework import serializers

from documents.models import Conversation, Document, Feedback, Message

MAX_QUESTION_LENGTH = 2000


class ChatRequestSerializer(serializers.Serializer):
    question = serializers.CharField(
        max_length=MAX_QUESTION_LENGTH, trim_whitespace=True
    )
    conversation_id = serializers.IntegerField(required=False, allow_null=True)
    document_type = serializers.ChoiceField(
        choices=Document.DocumentType.choices,
        required=False,
        allow_blank=True,
        default="",
    )
    department = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    # Ask about one document. Narrows retrieval after the permission filter.
    document_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    # Narrows to one collection, after authorization (spec 14).
    collection_id = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )


def _feedback_of_owner(messages):
    """{message id: {rating, comment}} - the conversation owner's ratings.

    "My" feedback is the owner's: a conversation is read and rated only by
    its owner (ConversationDetailView, FeedbackView), so the owner IS the
    reader, and no request is needed to know whose rating to show.
    """
    return {
        feedback.message_id: {"rating": feedback.rating, "comment": feedback.comment}
        for feedback in Feedback.objects.filter(
            message__in=[message.id for message in messages],
            user_id=F("message__conversation__owner_id"),
        ).only("message_id", "rating", "comment")
    }


class MessageListSerializer(serializers.ListSerializer):
    """Looks every message's rating up in one query, not one per message."""

    def to_representation(self, data):
        messages = list(data.all() if isinstance(data, BaseManager) else data)
        ratings = _feedback_of_owner(messages)
        for message in messages:
            message._my_feedback = ratings.get(message.id)
        return super().to_representation(messages)


class MessageSerializer(serializers.ModelSerializer):
    # So an answer rated earlier still shows its rating when the
    # conversation is reopened: {"rating", "comment"}, or null.
    my_feedback = serializers.SerializerMethodField()

    class Meta:
        model = Message
        list_serializer_class = MessageListSerializer
        fields = [
            "id",
            "role",
            "content",
            "abstained",
            "abstention_reason",
            "citations",
            "support_ratio",
            "model_name",
            "model_version",
            "generative",
            "retrieved",
            "passages_used",
            "duration_ms",
            "metadata",
            "created_at",
            "my_feedback",
        ]
        read_only_fields = fields

    def get_my_feedback(self, message):
        if hasattr(message, "_my_feedback"):
            return message._my_feedback
        return _feedback_of_owner([message]).get(message.id)


class ConversationSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = ["id", "title", "department", "created_at", "updated_at", "messages"]
        read_only_fields = fields


class ConversationSummarySerializer(serializers.ModelSerializer):
    message_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Conversation
        fields = [
            "id",
            "title",
            "department",
            "created_at",
            "updated_at",
            "message_count",
        ]
        read_only_fields = fields


class ConversationRenameSerializer(serializers.ModelSerializer):
    title = serializers.CharField(max_length=255, trim_whitespace=True)

    class Meta:
        model = Conversation
        fields = ["title"]
