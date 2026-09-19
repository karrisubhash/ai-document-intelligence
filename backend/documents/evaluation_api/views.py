from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents import authorization
from documents.evaluation import compare_runs
from documents.models import (
    Conversation,
    EvaluationCaseResult,
    EvaluationRun,
    Feedback,
    Message,
)
from documents.observability import telemetry

from .serializers import (
    EvaluationCaseResultSerializer,
    EvaluationRunSerializer,
    FeedbackSerializer,
)


def visible_runs(user):
    """Evaluation runs the caller may see.

    Scoped by the dataset's department, the same boundary documents use. A
    benchmark leaks the corpus it was built from through its questions and
    expected answers, so it is not public within the platform just because
    it happens to be a metric.
    """
    queryset = EvaluationRun.objects.select_related("dataset")

    if authorization.is_system_admin(user):
        return queryset

    profile = authorization.get_profile(user)
    if not profile.department:
        # Same rule as documents: an unset department is not a tenant.
        return queryset.none()

    return queryset.filter(dataset__department=profile.department)


class EvaluationRunListView(ListAPIView):
    serializer_class = EvaluationRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return visible_runs(self.request.user)


class EvaluationRunDetailView(RetrieveAPIView):
    serializer_class = EvaluationRunSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return visible_runs(self.request.user)


class EvaluationRunResultsView(ListAPIView):
    """Per-case results, so a bad aggregate can be traced to its cause."""

    serializer_class = EvaluationCaseResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        run = get_object_or_404(visible_runs(self.request.user), pk=self.kwargs["pk"])
        return EvaluationCaseResult.objects.filter(run=run).select_related("case")


class EvaluationCompareView(APIView):
    """Regression check between two runs (spec section 19.2)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk, other_pk):
        runs = visible_runs(request.user)
        baseline = get_object_or_404(runs, pk=pk)
        candidate = get_object_or_404(runs, pk=other_pk)
        return Response(compare_runs(baseline, candidate))


class FeedbackView(APIView):
    """Online quality signal on one answer (spec sections 15 and 20)."""

    permission_classes = [IsAuthenticated]

    def post(self, request, message_id):
        # Scoped through the conversation's owner: feedback is about an
        # answer produced under one user's authorization, so another user
        # has no standing to rate it - and should not learn it exists.
        message = get_object_or_404(
            Message,
            id=message_id,
            role=Message.Role.ASSISTANT,
            conversation__in=Conversation.objects.filter(owner=request.user),
        )

        serializer = FeedbackSerializer(data={**request.data, "message": message.id})
        serializer.is_valid(raise_exception=True)

        feedback, created = Feedback.objects.update_or_create(
            message=message,
            user=request.user,
            defaults={
                "rating": serializer.validated_data["rating"],
                "comment": serializer.validated_data.get("comment", ""),
                # Snapshot of what was judged: a later reprocess changes the
                # answer, and feedback about an answer that no longer exists
                # would otherwise silently re-attach to a different one.
                "answer_abstained": message.abstained,
                "answer_citations": len(message.citations or []),
            },
        )

        telemetry.feedback_recorded(feedback.rating)

        return Response(
            FeedbackSerializer(feedback).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
