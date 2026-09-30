from rest_framework import serializers

from documents.models import Conversation, Document, Message

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


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
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
        ]
        read_only_fields = fields


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
