"""The AI providers panel: the model chain's state, and a probe per link.

System administrators only, like the rest of System status: the chain names
providers and models, which is configuration, though never a key or a URL
(documents.rag.gateway.status).
"""

import time

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import exceptions, serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsSystemAdministrator
from documents.audit import record_event
from documents.models import AuditLog, Message
from documents.rag import budget, gateway
from documents.throttling import BurstUserThrottle

# What a probe asks. One output token is enough to prove the key, the model
# id and the endpoint all work; no document content is ever sent.
PROBE_MESSAGES = [{"role": "user", "content": "Reply with the word OK."}]


def answers_today() -> dict:
    """Assistant answers since midnight (server time zone), by how they were
    produced. From the messages themselves, so it survives a cache flush."""
    now = timezone.localtime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = Message.objects.filter(
        role=Message.Role.ASSISTANT, created_at__gte=start
    ).aggregate(
        # Aliased: an aggregate may not share a name with a model field.
        n_generative=Count("id", filter=Q(generative=True, abstained=False)),
        n_quoted=Count("id", filter=Q(generative=False, abstained=False)),
        n_abstained=Count("id", filter=Q(abstained=True)),
        # Quoted, or abstained, because no model could take the question.
        n_unavailable=Count("id", filter=Q(metadata__degraded__answerer=True)),
    )
    return {
        "generative": counts["n_generative"],
        "quoted": counts["n_quoted"],
        "abstained": counts["n_abstained"],
        "quoted_ai_unavailable": counts["n_unavailable"],
        "since": start.isoformat(),
    }


def overview() -> dict:
    """The chain and today's totals, as System status and this panel show."""
    status = gateway.status()
    status["totals"]["answers_today"] = answers_today()
    return status


class AIStatusView(APIView):
    """GET /api/v1/admin/ai/ - the chain on its own, cheap enough to poll.

    System status runs every dependency check (and writes a storage probe)
    on each load; the AI panel refreshes this instead while it is open.
    """

    permission_classes = [IsSystemAdministrator]

    def get(self, request):
        return Response({"checked_at": timezone.now(), **overview()})


class ProbeSerializer(serializers.Serializer):
    link = serializers.CharField(max_length=300)


class AIProbeView(APIView):
    """POST /api/v1/admin/ai/test/ {"link": "provider:model"}

    Sends a one-token request to that link, whatever its state - a bench or
    a spent day is exactly what an administrator may want to re-check - and
    reports what came back. It is a real request: it counts against the
    link's daily allowance like any other, which is why it is throttled
    tightly. A success lifts the link's bench; a 429 sets one. Audited.
    """

    permission_classes = [IsSystemAdministrator]
    throttle_classes = [BurstUserThrottle]
    throttle_scope = "ai_test"

    def post(self, request):
        serializer = ProbeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chain = gateway.default()
        link = next(
            (
                link
                for link in chain.links
                if link.id == serializer.validated_data["link"]
            ),
            None,
        )
        if link is None:
            raise exceptions.NotFound("No such link in the configured chain.")

        outcome = {"ok": False, "status_code": None, "error_type": None, "model": ""}
        started = time.monotonic()
        if not chain.configured(link):
            outcome["error_type"] = "unconfigured"
        else:
            try:
                completion = chain.run(
                    lambda target: {
                        "model": target.model,
                        "messages": PROBE_MESSAGES,
                        "temperature": 0,
                        gateway.TOKEN_LIMIT_FIELD: 1,
                        **dict(target.request_extras),
                    },
                    pin=link,
                )
                outcome["ok"] = True
                outcome["model"] = completion.served_by or completion.model
            except Exception as exc:
                # The class and the status only: provider error text can
                # echo the request and is not ours to display.
                outcome["error_type"] = type(exc).__name__
                outcome["status_code"] = budget._status(exc)
        latency_ms = int((time.monotonic() - started) * 1000)

        record_event(
            user=request.user,
            action=AuditLog.Action.AI_PROVIDER_TEST,
            result=AuditLog.Result.ALLOWED,
            link=link.id,
            ok=outcome["ok"],
            status_code=outcome["status_code"],
            error_type=outcome["error_type"],
        )

        row = next(row for row in chain.status()["chain"] if row["id"] == link.id)
        return Response(
            {"link": link.id, **outcome, "latency_ms": latency_ms, "state": row}
        )
