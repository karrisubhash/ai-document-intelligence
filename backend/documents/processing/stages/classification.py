from documents.processing.classification import classify
from documents.processing.classification.exceptions import (
    ClassificationError,
    ClassifierNotConfigured,
    ClassifierUnavailable,
)
from documents.processing.context import StageResult
from documents.processing.errors import ErrorCode, StageError, TransientError


def run(context) -> StageResult:
    """Classify the normalized content produced by the earlier stages.

    Reads DocumentContent, never the original upload - every downstream
    AI stage works from the same internal representation.
    """
    try:
        result = classify(
            context.content.id,
            processing_job=context.processing_job,
        )
    except ClassifierNotConfigured as exc:
        # Not an error: no model is wired up yet. Skipping is the honest
        # outcome - inventing a label would be worse than having none.
        return StageResult(message=str(exc), skipped=True)
    except ClassifierUnavailable as exc:
        raise TransientError(str(exc)) from exc
    except ClassificationError as exc:
        raise StageError(str(exc), code=ErrorCode.CLASSIFICATION_FAILED) from exc

    return StageResult(
        message=(
            f"{result.predicted_label} ({result.confidence:.2f}) "
            f"via {result.model_name} {result.model_version}"
        ),
        metadata={
            "predicted_label": result.predicted_label,
            "confidence": result.confidence,
            "model_name": result.model_name,
            "model_version": result.model_version,
            "classification_result_id": result.id,
        },
    )
