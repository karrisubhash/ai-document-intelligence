from rest_framework import serializers

from documents.models import (
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    Feedback,
)


class EvaluationDatasetSerializer(serializers.ModelSerializer):
    cases = serializers.IntegerField(source="cases.count", read_only=True)

    class Meta:
        model = EvaluationDataset
        fields = [
            "id",
            "name",
            "version",
            "description",
            "department",
            "cases",
            "created_at",
        ]
        read_only_fields = fields


class EvaluationRunSerializer(serializers.ModelSerializer):
    dataset = EvaluationDatasetSerializer(read_only=True)

    class Meta:
        model = EvaluationRun
        fields = [
            "id",
            "dataset",
            "label",
            "status",
            # Configuration travels with the metrics, always. A score
            # detached from what produced it cannot be compared to anything.
            "configuration",
            "metrics",
            "cases_total",
            "cases_scored",
            "duration_ms",
            "error_message",
            "started_at",
            "completed_at",
        ]
        read_only_fields = fields


class EvaluationCaseResultSerializer(serializers.ModelSerializer):
    question = serializers.CharField(source="case.question", read_only=True)
    expected_answer = serializers.CharField(
        source="case.expected_answer", read_only=True
    )
    expect_abstention = serializers.BooleanField(
        source="case.expect_abstention", read_only=True
    )
    relevant_documents = serializers.JSONField(
        source="case.relevant_documents", read_only=True
    )

    class Meta:
        model = EvaluationCaseResult
        fields = [
            "id",
            "question",
            "expected_answer",
            "expect_abstention",
            "relevant_documents",
            "answer",
            "abstained",
            "abstention_reason",
            "retrieved_documents",
            "cited_documents",
            "scores",
            "duration_ms",
        ]
        read_only_fields = fields


class FeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feedback
        fields = [
            "id",
            "message",
            "rating",
            "comment",
            "answer_abstained",
            "answer_citations",
            "created_at",
        ]
        read_only_fields = [
            "id",
            # Taken from the URL by FeedbackView, never from the body.
            "message",
            "answer_abstained",
            "answer_citations",
            "created_at",
        ]
