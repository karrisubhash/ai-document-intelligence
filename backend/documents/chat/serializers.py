from rest_framework import serializers

from documents.models import Conversation, Document, Message
from documents.rag import labels

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


class MessageSerializer(serializers.ModelSerializer):
    # The author for people and what ran in a lesser mode, as the chat
    # payload gives them (documents.rag.labels); derived for answers stored
    # before either was recorded.
    model_display = serializers.SerializerMethodField()
    degraded = serializers.SerializerMethodField()

    def get_model_display(self, message) -> str:
        if message.role != Message.Role.ASSISTANT:
            return ""
        return labels.answer_display(
            model_name=message.model_name,
            model_version=message.model_version,
            generative=message.generative,
            llm=(message.metadata or {}).get("llm"),
        )

    def get_degraded(self, message):
        if message.role != Message.Role.ASSISTANT:
            return None
        metadata = message.metadata or {}
        if isinstance(metadata.get("degraded"), dict):
            return metadata["degraded"]
        return labels.degraded(
            answerer=False,
            reranker=False,
            source=metadata.get("source", ""),
            notes=metadata.get("notes") or (),
        )

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
            "model_display",
            "degraded",
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
