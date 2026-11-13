import time

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.audit import record_event
from documents.models import AuditLog
from documents.throttling import BurstUserThrottle, ShortWindowUserThrottle

from . import presentation, ranking
from .serializers import SearchRequestSerializer, max_limit


class SearchView(APIView):
    """Hybrid search over chunks the caller is authorized to read, reranked,
    showing only passages relevant enough to be worth reading (ranking.py).

    POST rather than GET because a query is user content, and putting it in
    a URL would spread it through access logs and browser history - the
    spec's "do not expose confidential document content through ordinary
    application logs" applies to what people search for too.
    """

    permission_classes = [IsAuthenticated]
    # An hourly budget, and a per-minute limit on how fast it may be spent:
    # a reranked search costs seconds of CPU (ShortWindowUserThrottle).
    throttle_classes = [BurstUserThrottle, ShortWindowUserThrottle]
    throttle_scope = "search"
    throttle_short_window_scope = "search_burst"

    def post(self, request):
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        started = time.monotonic()

        response = ranking.search(
            request.user,
            data["query"],
            limit=data["limit"],
            include_below_floor=data["include_below_floor"],
            document_type=data.get("document_type", ""),
            department=data.get("department", ""),
            document_id=data.get("document_id"),
            collection_id=data.get("collection_id"),
        )

        # Presentation only: the hits keep the order and the selection the
        # ranking gave them (search.presentation).
        spans = presentation.highlight_spans(response.hits, response.query)
        results = [
            presentation.hit_payload(hit, spans.get(hit.chunk.id, []))
            for hit in response.hits
        ]

        record_event(
            user=request.user,
            action=AuditLog.Action.SEARCH,
            result=AuditLog.Result.ALLOWED,
            # The query itself is deliberately NOT recorded - it is user
            # content, and an audit trail should say that a search happened
            # without becoming a second copy of what people looked for.
            hits=len(response.hits),
            authorized_documents=response.authorized_documents,
        )

        body = {
            "query": response.query,
            "count": len(response.hits),
            "limit": data["limit"],
            "max_limit": max_limit(),
            "semantic": response.semantic,
            "embedding_model": response.embedding_model,
            "retrieval": {
                "lexical_candidates": response.lexical_candidates,
                "vector_candidates": response.vector_candidates,
                "authorized_documents": response.authorized_documents,
            },
            # Empty `reranker` means the fused order was returned as is
            # (SEARCH_RERANK_DEPTH=0, or nothing was retrieved).
            "ranking": {
                "reranker": response.reranker,
                "degraded": response.reranker_degraded,
                "relevance_floor": response.relevance_floor,
                "reranked": response.reranked,
                "below_floor": response.below_floor,
            },
            # Diagnostics for operators; `notices` are the same situations as
            # codes for the client to word (docs/api/reference.md).
            "notes": response.notes,
            "notices": response.notices,
            "results": results,
            "server_ms": round((time.monotonic() - started) * 1000),
        }
        if data["group_by"] == "document":
            body["groups"] = presentation.group_by_document(
                [result for result in results if not result["below_floor"]]
            )
        return Response(body, status=status.HTTP_200_OK)
