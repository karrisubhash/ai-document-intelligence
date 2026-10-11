import json
import logging
import queue
import threading

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connections, transaction
from django.db.models import Count
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents import agents
from documents.audit import record_event
from documents.authorization import get_profile
from documents.models import AuditLog, Conversation, Message
from documents.rag import followup, general, streaming
from documents.throttling import BurstUserThrottle

from .serializers import (
    ChatRequestSerializer,
    ConversationRenameSerializer,
    ConversationSerializer,
    ConversationSummarySerializer,
)

# A conversation is titled from its first question until its owner renames
# it. Truncated at a word boundary so the list reads as titles, not as
# questions cut mid-word.
logger = logging.getLogger(__name__)

TITLE_LENGTH = 80


def _title_from(question: str) -> str:
    question = " ".join(question.split())
    if len(question) <= TITLE_LENGTH:
        return question
    cut = question[:TITLE_LENGTH].rsplit(" ", 1)[0]
    return f"{cut}..."


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
        return Response(
            self.handle(request, serializer.validated_data), status=status.HTTP_200_OK
        )

    def handle(self, request, data) -> dict:
        """Answer one chat turn and persist it; the response payload.

        Shared by this JSON endpoint and ChatStreamView, which runs it in a
        worker thread with a streaming sink installed.
        """
        created = not data.get("conversation_id")
        conversation = self._conversation(request, data.get("conversation_id"))

        try:
            # A follow-up is resolved against the caller's own earlier turns
            # before retrieval sees it. "What about MECH?" carries one
            # content term and retrieves nothing useful on its own; the
            # conversation is where the rest of that question already lives.
            # Independent questions are returned untouched - see
            # documents.rag.followup.
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
                document_id=data.get("document_id"),
                collection_id=data.get("collection_id"),
                # The user's own words, when the question was widened as a
                # follow-up: retrieval searched on the widened form, the
                # answer must still be about what was actually asked.
                focus=resolved.original if resolved.rewritten else "",
            )
            source = "none" if answer.abstained else "documents"

            # Opt-in (CHAT_GENERAL_KNOWLEDGE): when the documents had
            # nothing, reply from general knowledge instead of refusing -
            # labelled, uncited, and never after a grounding failure. See
            # documents.rag.general for the rules.
            if general.should_answer(answer):
                reply = general.answer(
                    data["question"],
                    history=conversation.messages.order_by("created_at"),
                )
                if reply is not None:
                    answer.notes.append(
                        f"not found in your documents ({answer.abstention_reason}); "
                        f"answered from general knowledge"
                    )
                    answer.answer = reply.text
                    answer.abstained = False
                    answer.abstention_reason = ""
                    answer.citations = []
                    answer.generative = True
                    answer.model_name = reply.model_name
                    answer.model_version = reply.model_version
                    source = "general"
        except Exception:
            # Created for this question, which was never answered: left in
            # place it was an empty "Untitled conversation" in the list.
            if created:
                conversation.delete()
            raise

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
                    "source": source,
                },
            )
            # touch() so the conversation sorts by recency.
            update_fields = ["updated_at"]
            if not conversation.title:
                conversation.title = _title_from(data["question"])
                update_fields.append("title")
            conversation.save(update_fields=update_fields)

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

        return {
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
            # Where the answer came from: "documents" (grounded, cited),
            # "general" (general knowledge, uncited - see
            # documents.rag.general) or "none" (an abstention).
            "source": source,
        }

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


class ChatStreamView(ChatView):
    """POST /api/v1/chat/stream/ - the same turn as ChatView, streamed.

    Server-Sent Events, in order:

      event: delta   {"text": ...}   the model's words as it writes them
      event: reset   {}              discard what has been shown so far
      event: done    {ChatView payload}
      event: error   {"detail": ...}

    Deltas are PROVISIONAL (see documents.rag.streaming): grounding runs
    after generation, so `done` is the authoritative answer and a client
    renders that. An answerer that cannot stream (the extractive baseline)
    sends no deltas, only `done`.

    The turn runs in a worker thread so the response can be written while
    it is generated. Validation, authentication, throttling and the
    conversation-ownership check all happen first, on the request thread,
    so they fail as ordinary JSON 4xx responses rather than inside a stream.
    """

    KEEPALIVE_SECONDS = 15

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("conversation_id"):
            self._conversation(request, data["conversation_id"])  # 404 now

        events = queue.Queue()

        def sink(event, text):
            events.put((event, {"text": text} if event == "delta" else {}))

        def work():
            try:
                with streaming.capture(sink):
                    events.put(("done", self.handle(request, data)))
            except Exception:
                logger.exception("chat.stream_failed")
                events.put(
                    (
                        "error",
                        {
                            "detail": "The answer could not be completed. Please try again."
                        },
                    )
                )
            finally:
                # Connections are per thread; this one's would otherwise
                # stay open until the process exits.
                connections.close_all()
                events.put(None)

        threading.Thread(target=work, name="chat-stream", daemon=True).start()

        response = StreamingHttpResponse(
            self._events(events), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        # nginx buffers proxied responses by default, which would hold every
        # delta until the answer was complete. This turns it off for this
        # response only.
        response["X-Accel-Buffering"] = "no"
        return response

    def _events(self, events):
        while True:
            try:
                item = events.get(timeout=self.KEEPALIVE_SECONDS)
            except queue.Empty:
                # Retrieval and reranking can take seconds before the first
                # word; a comment line keeps idle-timeout proxies patient.
                yield ": keep-alive\n\n"
                continue
            if item is None:
                return
            event, payload = item
            data = json.dumps(payload, cls=DjangoJSONEncoder)
            yield f"event: {event}\ndata: {data}\n\n"


class ConversationListView(ListAPIView):
    """The caller's own conversations, most recent first. Never anyone
    else's - a conversation is a record of what was disclosed to one user
    under their own authorization."""

    serializer_class = ConversationSummarySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            Conversation.objects.filter(owner=self.request.user)
            .annotate(message_count=Count("messages"))
            .order_by("-updated_at", "-id")
        )


class ConversationDetailView(APIView):
    """Read, rename or delete one of the caller's conversations.

    Another user's id is a 404, not a 403, for the same reason documents
    are: a conversation's existence is itself something only its owner
    should be able to confirm.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        conversation = get_object_or_404(Conversation, id=pk, owner=request.user)
        return Response(ConversationSerializer(conversation).data)

    def patch(self, request, pk):
        conversation = get_object_or_404(Conversation, id=pk, owner=request.user)
        serializer = ConversationRenameSerializer(conversation, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(ConversationSerializer(conversation).data)

    def delete(self, request, pk):
        conversation = get_object_or_404(Conversation, id=pk, owner=request.user)
        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
