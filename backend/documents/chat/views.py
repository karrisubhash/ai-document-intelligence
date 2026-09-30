from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents import agents
from documents.audit import record_event
from documents.authorization import get_profile
from documents.models import AuditLog, Conversation, Message
from documents.rag import followup
from documents.throttling import BurstUserThrottle

from .serializers import (
    ChatRequestSerializer,
    ConversationSerializer,
)


class ChatView(APIView):
    """Ask a question over the documents the caller is authorized to read.

    POST rather than GET for the same reason search is: a question is user
    content, and a URL spreads it through access logs and browser history.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [BurstUserThrottle]
    throttle_scope = "chat"

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        conversation = self._conversation(request, data.get("conversation_id"))

        # A follow-up is resolved against the caller's own earlier turns
        # before retrieval sees it. "What about MECH?" carries one content
        # term and retrieves nothing useful on its own; the conversation is
        # where the rest of that question already lives. Independent
        # questions are returned untouched - see documents.rag.followup.
        resolved = followup.contextualize(
            data["question"],
            conversation.messages.order_by("created_at"),
        )

        # Planned when the question asks for several things, and a plain
        # single retrieval otherwise - which is the overwhelmingly common
        # case and behaves exactly as it did before planning existed.
        answer = agents.answer(
            request.user,
            resolved.question,
            document_type=data.get("document_type", ""),
            department=data.get("department", ""),
        )

        with transaction.atomic():
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.USER,
                content=data["question"],
            )
            assistant = Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=answer.answer,
                abstained=answer.abstained,
                abstention_reason=answer.abstention_reason,
                citations=answer.citations,
                support_ratio=answer.support_ratio,
                model_name=answer.model_name,
                model_version=answer.model_version,
                generative=answer.generative,
                retrieved=answer.retrieved,
                passages_used=answer.passages_used,
                duration_ms=answer.duration_ms,
                metadata={
                    "reranker": answer.reranker_name,
                    "semantic_retrieval": answer.semantic_retrieval,
                    "suspected_injections": answer.suspected_injections,
                    "grounding_failures": answer.grounding_failures,
                    "notes": answer.notes,
                    "planned": answer.planned,
                    "plan": [step.question for step in answer.steps],
                    "unanswered": answer.unanswered,
                    # How a follow-up was resolved, so an answer to the
                    # wrong reading of a question can be traced to the
                    # rewrite that caused it rather than blamed on
                    # retrieval.
                    "followup": resolved.as_metadata,
                    "timings": answer.timings,
                },
            )
            # touch() so the conversation sorts by recency.
            conversation.save(update_fields=["updated_at"])

        record_event(
            user=request.user,
            action=AuditLog.Action.CHAT,
            result=AuditLog.Result.ALLOWED,
            # The question is deliberately not recorded - see the search
            # endpoint for the same reasoning.
            conversation_id=conversation.id,
            abstained=answer.abstained,
            citations=len(answer.citations),
        )

        return Response(
            {
                "conversation_id": conversation.id,
                # What the caller asked, not what retrieval searched on. A
                # UI echoing this back must show the user their own words;
                # the resolved form is reported beside it rather than in
                # place of it.
                "question": resolved.original,
                "resolved_question": (resolved.question if resolved.rewritten else ""),
                "answer": answer.answer,
                "abstained": answer.abstained,
                "abstention_reason": answer.abstention_reason,
                "citations": answer.citations,
                "grounding": {
                    "support_ratio": answer.support_ratio,
                    "failures": answer.grounding_failures,
                },
                "retrieval": {
                    "retrieved": answer.retrieved,
                    "reranked": answer.reranked,
                    "passages_used": answer.passages_used,
                    "semantic": answer.semantic_retrieval,
                    "reranker": answer.reranker_name,
                    "suspected_injections": answer.suspected_injections,
                },
                "model": {
                    "name": answer.model_name,
                    "version": answer.model_version,
                    "generative": answer.generative,
                },
                "plan": {
                    # Reported whether or not the question was planned, so
                    # a caller can always tell which it got rather than
                    # inferring it from the shape of the answer.
                    "planned": answer.planned,
                    "steps": [
                        {
                            "question": step.question,
                            "abstained": step.abstained,
                            "abstention_reason": step.abstention_reason,
                        }
                        for step in answer.steps
                    ],
                    "unanswered": answer.unanswered,
                },
                "duration_ms": answer.duration_ms,
                "timings": answer.timings,
                "notes": answer.notes,
                "message_id": assistant.id,
            },
            status=status.HTTP_200_OK,
        )

    def _conversation(self, request, conversation_id):
        if conversation_id:
            # Scoped to the caller: a conversation holds answers produced
            # under one user's authorization, so it is never shared by id.
            return get_object_or_404(
                Conversation, id=conversation_id, owner=request.user
            )

        return Conversation.objects.create(
            owner=request.user,
            department=get_profile(request.user).department,
        )


class ConversationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        conversation = get_object_or_404(Conversation, id=pk, owner=request.user)
        return Response(ConversationSerializer(conversation).data)
