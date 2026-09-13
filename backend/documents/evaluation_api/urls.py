from django.urls import path

from .views import (
    EvaluationCompareView,
    EvaluationRunDetailView,
    EvaluationRunListView,
    EvaluationRunResultsView,
)

urlpatterns = [
    path("", EvaluationRunListView.as_view(), name="evaluation-list"),
    path("<int:pk>/", EvaluationRunDetailView.as_view(), name="evaluation-detail"),
    path(
        "<int:pk>/results/",
        EvaluationRunResultsView.as_view(),
        name="evaluation-results",
    ),
    path(
        "<int:pk>/compare/<int:other_pk>/",
        EvaluationCompareView.as_view(),
        name="evaluation-compare",
    ),
]
