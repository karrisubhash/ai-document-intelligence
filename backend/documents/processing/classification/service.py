import functools

from django.conf import settings
from django.utils.module_loading import import_string

from documents.models import ClassificationResult, DocumentContent

from .base import ClassificationPrediction, DocumentClassifier
from .exceptions import ClassificationError, ClassifierNotConfigured


@functools.cache
def _load_classifier(path: str) -> DocumentClassifier:
    """Instantiate once per dotted path - loading model weights on every
    document would dominate inference cost."""
    return import_string(path)()


def reset_classifier_cache() -> None:
    _load_classifier.cache_clear()


def get_classifier() -> DocumentClassifier:
    path = getattr(settings, "DOCUMENT_CLASSIFIER", "")

    if not path:
        raise ClassifierNotConfigured(
            "No DOCUMENT_CLASSIFIER configured. Classification is skipped "
            "rather than guessed."
        )

    return _load_classifier(path)


def _validate(prediction: ClassificationPrediction) -> None:
    # The boundary between our pipeline and a model we did not write.
    if not 0.0 <= prediction.confidence <= 1.0:
        raise ClassificationError(
            f"Classifier returned confidence {prediction.confidence!r}, "
            f"which is outside [0, 1]."
        )
    if not prediction.label:
        raise ClassificationError("Classifier returned an empty label.")


def classify(document_content_id: int, *, processing_job=None) -> ClassificationResult:
    """Classify one normalized DocumentContent and persist the verdict.

    Raises ClassifierNotConfigured when nothing is wired up; callers
    decide whether that is fatal or simply means "not available yet".
    """
    content = DocumentContent.objects.select_related("document_version").get(
        id=document_content_id
    )

    classifier = get_classifier()
    prediction = classifier.predict(document_content_id=document_content_id)
    _validate(prediction)

    return ClassificationResult.objects.create(
        document_version=content.document_version,
        document_content=content,
        processing_job=processing_job,
        model_name=prediction.model_name,
        model_version=prediction.model_version,
        predicted_label=prediction.label,
        confidence=prediction.confidence,
        input_reference=prediction.input_reference,
    )
