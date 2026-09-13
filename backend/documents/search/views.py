from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.audit import record_event
from documents.models import AuditLog
from documents.throttling import BurstUserThrottle

from . import service
from .serializers import SearchRequestSerializer


class SearchView(APIView):
    """Hybrid search over chunks the caller is authorized to read.

    POST rather than GET because a query is user content, and putting it in
    a URL would spread it through access logs and browser history - the
    spec's "do not expose confidential document content through ordinary
    application logs" applies to what people search for too.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [BurstUserThrottle]
    throttle_scope = "search"

    def post(self, request):
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        response = service.search(
            request.user,
            data["query"],
            limit=data["limit"],
            document_type=data.get("document_type", ""),
            department=data.get("department", ""),
        )

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

        return Response(
            {
                "query": response.query,
                "count": len(response.hits),
                "semantic": response.semantic,
                "embedding_model": response.embedding_model,
                "retrieval": {
                    "lexical_candidates": response.lexical_candidates,
                    "vector_candidates": response.vector_candidates,
                    "authorized_documents": response.authorized_documents,
                },
                "notes": response.notes,
                "results": [
                    {
                        **hit.citation,
                        "score": hit.score,
                        "matched_by": list(hit.matched_by),
                        "lexical_rank": hit.lexical_rank,
                        "vector_rank": hit.vector_rank,
                    }
                    for hit in response.hits
                ],
            },
            status=status.HTTP_200_OK,
        )
