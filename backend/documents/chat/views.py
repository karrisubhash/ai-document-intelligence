import contextvars
import json
import logging
import queue
import threading
import time

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connections, transaction
from django.db.models import Count
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents import agents
from documents.agents.runner import PlannedAnswer
from documents.api.serializers import BULK_DELETE_LIMIT, BulkSelectionSerializer
from documents.audit import record_event
from documents.authorization import get_profile
from documents.models import AuditLog, Conversation, Message
from documents.observability import telemetry
from documents.rag import followup, gateway, general, labels, streaming
from documents.rag.engine import Abstention, llm_metadata
from documents.rag.llm import ABSTENTION_TEXT
from documents.throttling import BurstUserThrottle, ShortWindowUserThrottle

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
    # An hourly budget, and a per-minute limit on how fast it may be spent
    # (ShortWindowUserThrottle). ChatStreamView inherits both.
    throttle_classes = [BurstUserThrottle, ShortWindowUserThrottle]
    throttle_scope = "chat"
    throttle_short_window_scope = "chat_burst"

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
            # Read once, and only what follow-up resolution and the general
            # reply use. Each assistant turn also stores its citations - the
            # cited passages' full text - and its metadata. Follow-up
            # resolution used to fetch all of that, for every turn, on every
            # question, and the general reply fetched it again whenever it
            # answered.
            history = list(
                conversation.messages.order_by("created_at").only("role", "content")
            )

            # A follow-up is resolved against the caller's own earlier turns
            # before retrieval sees it. "What about MECH?" carries one
            # content term and retrieves nothing useful on its own; the
            # conversation is where the rest of that question already lives.
            # Independent questions are returned untouched - see
            # documents.rag.followup.
            resolved = followup.contextualize(data["question"], history)
            small_talk = followup.is_small_talk(data["question"]) and general.enabled()
            if small_talk:
                # A greeting needs no document search. Recorded as a clean
                # "nothing in the documents", which the general-knowledge
                # reply below answers.
                answer = PlannedAnswer(
                    question=data["question"],
                    answer=ABSTENTION_TEXT,
                    abstained=True,
                    abstention_reason=Abstention.NO_RELEVANT_PASSAGES,
                    notes=["small talk; documents not searched"],
                )
            else:
                # Planned when the question asks for several things, and a
                # plain single retrieval otherwise - which is the
                # overwhelmingly common case and behaves exactly as it did
                # before planning existed.
                answer = agents.answer(
                    request.user,
                    resolved.question,
                    document_type=data.get("document_type", ""),
                    department=data.get("department", ""),
                    document_id=data.get("document_id"),
                    collection_id=data.get("collection_id"),
                    # The user's own words, when the question was widened as
                    # a follow-up: retrieval searched on the widened form,
                    # the answer must still be about what was actually asked.
                    focus=resolved.original if resolved.rewritten else "",
                )
            source = "none" if answer.abstained else "documents"

            # Opt-in (CHAT_GENERAL_KNOWLEDGE): when the documents had
            # nothing, reply from general knowledge instead of refusing -
            # labelled, uncited, and never after a grounding failure. See
            # documents.rag.general for the rules.
            if general.should_answer(answer):
                reply = general.answer(data["question"], history=history)
                if reply is not None:
                    if not small_talk:
                        answer.notes.append(
                            f"not found in your documents "
                            f"({answer.abstention_reason}); "
                            f"answered from general knowledge"
                        )
                    answer.answer = reply.text
                    answer.abstained = False
                    answer.abstention_reason = ""
                    answer.citations = []
                    answer.generative = True
                    answer.model_name = reply.model_name
                    answer.model_version = reply.model_version
                    answer.llm = llm_metadata(reply)
                    source = "general"
        except Exception:
            # Created for this question, which was never answered: left in
            # place it was an empty "Untitled conversation" in the list.
            if created:
                conversation.delete()
            raise

        # Said in the answer itself rather than buried in its notes: which
        # model wrote it, and whether it was quoted because the AI was at
        # capacity (documents.rag.labels).
        degraded = labels.degraded(
            answerer=answer.answerer_fell_back,
            reranker=answer.reranker_degraded,
            llm=answer.llm,
            source=source,
        )
        model_display = labels.answer_display(
            model_name=answer.model_name,
            model_version=answer.model_version,
            generative=answer.generative,
            llm=answer.llm,
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
                    "source": source,
                    # What the provider said: model actually used, what it
                    # fell back from, tokens, finish reason, data use.
                    "llm": answer.llm,
                    "degraded": degraded,
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
            # The author for people ("Gemini 3.5 Flash", "GPT-OSS 120B ·
            # Groq", "Quoted from your documents"); `model.name` stays the
            # answerer's class for anything that reads it.
            "model_display": model_display,
            # {answerer, reranker, reason}: what ran in a lesser mode, and
            # whether it was capacity (comes back) or a failure.
            "degraded": degraded,
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
      event: reset   {source?}       discard what has been shown so far;
                                     `source: "general"` labels what follows
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

    def perform_content_negotiation(self, request, force=False):
        # A browser asks for `Accept: text/event-stream`, which no DRF
        # renderer produces - the stream is a plain StreamingHttpResponse
        # that bypasses renderers. Without `force`, DRF's negotiation runs
        # before authentication and refused every real browser request
        # with 406 "Could not satisfy the request Accept header" (found in
        # the web app; the tests had not sent the header). Forced, it falls
        # back to the first renderer, so the errors raised BEFORE streaming
        # - validation, authentication, throttling, 404 - are still JSON.
        return super().perform_content_negotiation(request, force=True)

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("conversation_id"):
            self._conversation(request, data["conversation_id"])  # 404 now

        events = queue.Queue()

        def sink(event, text):
            if event == "delta":
                events.put((event, {"text": text}))
            else:
                # `reset` may name the source of the answer that follows, so
                # a general-knowledge reply is labelled while it streams.
                events.put((event, {"source": text} if text else {}))

        def work():
            try:
                with streaming.capture(sink):
                    events.put(("done", self.handle(request, data)))
            except Exception:
                logger.exception("chat.stream_failed")
                telemetry.chat_stream_failed()
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

        # threading.Thread does not inherit contextvars: without the copy,
        # every log line and tracing span from the turn lost the request id
        # (and ObservabilityMiddleware resets it on this thread anyway once
        # the response is returned). The sink is set inside the copy.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run, args=(work,), name="chat-stream", daemon=True
        ).start()

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


class ConversationBulkDeleteView(APIView):
    """Delete several of the caller's conversations, or all of them.

    Only ever the caller's own: another user's id is `not_found`, exactly
    like one that does not exist, as on ConversationDetailView. Documents
    are not affected. `all` works like documents' (see
    BulkSelectionSerializer): at most BULK_DELETE_LIMIT per request - each
    takes its messages and their feedback with it - with `remaining` and
    `as_of`.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = BulkSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        mine = Conversation.objects.filter(owner=request.user)
        if data["all"]:
            as_of = data.get("as_of") or timezone.now()
            mine = mine.filter(created_at__lte=as_of)
            chosen = list(
                mine.order_by("pk").values_list("pk", flat=True)[:BULK_DELETE_LIMIT]
            )
        else:
            chosen = data["ids"]
        with transaction.atomic():
            # Locked, so what is reported deleted is what this request deleted.
            deleted = sorted(
                mine.filter(pk__in=chosen)
                .select_for_update()
                .values_list("pk", flat=True)
            )
            Conversation.objects.filter(pk__in=deleted).delete()

        body = {
            "deleted": deleted,
            "not_found": [] if data["all"] else sorted(set(chosen) - set(deleted)),
        }
        if data["all"]:
            body["remaining"] = mine.count()
            body["as_of"] = as_of
        return Response(body)


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


class ChatCapabilitiesView(APIView):
    """GET /api/v1/chat/capabilities/ - what chat can do right now.

    For the composer: whether answers are being written by a model or, with
    every model cooling down or out of today's quota, quoted from the
    documents - and when writing resumes. No provider, model or key is
    named; that is the system administrators' System status screen.

    Cheap by construction (a few cache reads, no network), and remembered
    for CACHE_SECONDS per process on top, because every open chat page asks.
    Never audited: it is a read of service state, not of anyone's data.
    """

    permission_classes = [IsAuthenticated]
    CACHE_SECONDS = 5.0

    _lock = threading.Lock()
    _cached = (0.0, None)

    def get(self, request):
        now = time.monotonic()
        with self._lock:
            stamp, body = ChatCapabilitiesView._cached
        if body is None or now - stamp > self.CACHE_SECONDS:
            body = gateway.capabilities()
            with self._lock:
                ChatCapabilitiesView._cached = (now, body)
        response = Response(body)
        response["Cache-Control"] = f"private, max-age={int(self.CACHE_SECONDS)}"
        return response
