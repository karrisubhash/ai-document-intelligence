import functools

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from documents.models import ClassificationResult, Document, DocumentContent

from .base import ClassificationPrediction, DocumentClassifier
from .exceptions import ClassificationError, ClassifierNotConfigured


@functools.cache
def _load_classifier(path: str, artifact: str) -> DocumentClassifier:
    """Instantiate once per (dotted path, artifact) pair.

    Loading model weights on every document would dominate inference cost,
    so instances are cached. `artifact` is part of the key rather than a
    constructor argument: the adapter reads the setting itself, but keying
    on the dotted path alone would keep serving the previously loaded
    model after the artifact setting was pointed at a new one.
    """
    return import_string(path)()


@functools.cache
def _load_type_detector(path: str) -> DocumentClassifier:
    """Instantiate once per dotted path; it caches prototype vectors."""
    return import_string(path)()


def reset_classifier_cache() -> None:
    _load_classifier.cache_clear()
    _load_type_detector.cache_clear()


def get_classifier() -> DocumentClassifier:
    path = getattr(settings, "DOCUMENT_CLASSIFIER", "")

    if not path:
        raise ClassifierNotConfigured(
            "No DOCUMENT_CLASSIFIER configured. Classification is skipped "
            "rather than guessed."
        )

    return _load_classifier(path, getattr(settings, "DOCUMENT_CLASSIFIER_ARTIFACT", ""))


def get_type_detector() -> DocumentClassifier:
    """The institutional type detector, or ClassifierNotConfigured when off."""
    path = getattr(settings, "DOCUMENT_TYPE_DETECTOR", "")

    if not path:
        raise ClassifierNotConfigured(
            "No DOCUMENT_TYPE_DETECTOR configured. Document types are left as uploaded."
        )

    return _load_type_detector(path)


def _validate(prediction: ClassificationPrediction) -> None:
    # The boundary between our pipeline and a model we did not write.
    if not 0.0 <= prediction.confidence <= 1.0:
        raise ClassificationError(
            f"Classifier returned confidence {prediction.confidence!r}, "
            f"which is outside [0, 1]."
        )
    if not prediction.label:
        raise ClassificationError("Classifier returned an empty label.")


def _abstained(prediction: ClassificationPrediction) -> bool:
    return prediction.is_unknown or bool(prediction.metadata.get("abstained"))


def classify(document_content_id: int, *, processing_job=None) -> ClassificationResult:
    """Classify one normalized DocumentContent and persist the verdict.

    Raises ClassifierNotConfigured when nothing is wired up; callers
    decide whether that is fatal or simply means "not available yet".

    This is the configured DOCUMENT_CLASSIFIER - a layout classifier such as
    the RVL-CDIP text baseline unless the class declares otherwise - and its
    verdict never sets the document's type. See detect_document_type.
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
        taxonomy=getattr(
            classifier, "taxonomy", ClassificationResult.Taxonomy.LAYOUT_CLASS
        ),
        predicted_label=prediction.label,
        confidence=prediction.confidence,
        abstained=_abstained(prediction),
        input_reference=prediction.input_reference,
        metadata=prediction.metadata,
    )


class TypeOutcome:
    """What a type verdict did to the document - recorded on the event."""

    APPLIED = "applied"
    # A person chose the type; the detector's verdict is on record only.
    USER_CHOICE_KEPT = "user_choice_kept"
    # The detector had set the type from an earlier version or pass, and is
    # no longer confident: the type goes back to GENERAL rather than
    # keeping a decision the current content does not support.
    REVERTED = "reverted_to_default"
    UNCHANGED = "unchanged"
    # The job is for a version that is no longer the document's current one.
    SUPERSEDED = "superseded"
    # DOCUMENT_TYPE_AUTO_APPLY is off: recorded as a suggestion only.
    SUGGESTION_ONLY = "suggestion_only"


def detect_document_type(document_content_id: int, *, processing_job=None):
    """Run the institutional type detector, persist its verdict, apply it.

    Returns (ClassificationResult, TypeOutcome). Raises
    ClassifierNotConfigured when the detector is switched off.
    """
    content = DocumentContent.objects.select_related("document_version").get(
        id=document_content_id
    )

    detector = get_type_detector()
    prediction = detector.predict(document_content_id=document_content_id)
    _validate(prediction)

    result = ClassificationResult.objects.create(
        document_version=content.document_version,
        document_content=content,
        processing_job=processing_job,
        model_name=prediction.model_name,
        model_version=prediction.model_version,
        taxonomy=ClassificationResult.Taxonomy.DOCUMENT_TYPE,
        predicted_label=prediction.label,
        confidence=prediction.confidence,
        abstained=_abstained(prediction),
        input_reference=prediction.input_reference,
        metadata=prediction.metadata,
    )

    outcome = apply_document_type(content.document_version, result)
    return result, outcome


def apply_document_type(version, result: ClassificationResult) -> str:
    """Set the document's type from a verdict - only where that is allowed.

    Allowed when the type is still the upload default or was set by the
    detector itself, and only for the document's CURRENT version: a verdict
    on a superseded version says nothing about the file people now read.
    A person's choice (`document_type_source == "user"`) is never touched.

    Each decision is one conditional UPDATE, so it cannot interleave with a
    concurrent edit: a PATCH that lands first marks the type "user" and this
    then matches nothing.
    """
    Source = Document.TypeSource
    current = Document.objects.filter(
        pk=version.document_id, current_version_id=version.pk
    )
    now = timezone.now()

    if result.abstained:
        reverted = current.filter(document_type_source=Source.AUTO).update(
            document_type=Document.DocumentType.GENERAL,
            document_type_source=Source.DEFAULT,
            type_confidence=None,
            updated_at=now,
        )
        if reverted:
            return TypeOutcome.REVERTED
        return TypeOutcome.UNCHANGED if current.exists() else TypeOutcome.SUPERSEDED

    if result.predicted_label not in Document.DocumentType.values:
        return TypeOutcome.UNCHANGED

    if not getattr(settings, "DOCUMENT_TYPE_AUTO_APPLY", True):
        return TypeOutcome.SUGGESTION_ONLY

    applied = current.filter(
        document_type_source__in=(Source.DEFAULT, Source.AUTO)
    ).update(
        document_type=result.predicted_label,
        document_type_source=Source.AUTO,
        type_confidence=result.confidence,
        updated_at=now,
    )
    if applied:
        return TypeOutcome.APPLIED
    # Read after the write, not before it: the reason is whatever made the
    # UPDATE match nothing, including a version uploaded a moment ago.
    return TypeOutcome.USER_CHOICE_KEPT if current.exists() else TypeOutcome.SUPERSEDED
