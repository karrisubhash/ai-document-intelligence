import logging

from documents.models import Document
from documents.processing.classification import (
    TypeOutcome,
    classify,
    detect_document_type,
)
from documents.processing.classification.exceptions import (
    ClassificationError,
    ClassifierNotConfigured,
    ClassifierUnavailable,
)
from documents.processing.context import StageResult
from documents.processing.errors import ErrorCode, StageError, TransientError

logger = logging.getLogger("adi.pipeline")


def run(context) -> StageResult:
    """Classify the normalized content produced by the earlier stages.

    Two classifiers, two taxonomies, one stage - and it runs BEFORE
    EXTRACTION on purpose, because extraction selects its schema by the
    document's type:

    1. The institutional type detector (DOCUMENT_TYPE_DETECTOR, on by
       default) predicts Document.DocumentType and, when the uploader left
       the default and the detector is confident, sets it. It never fails
       the document: a document it cannot type is a GENERAL document, and a
       fault in it is recorded and logged, not raised.
    2. A configured DOCUMENT_CLASSIFIER (a layout classifier such as the
       RVL-CDIP text baseline) records its verdict as a layout class. Its
       failure modes are unchanged: unconfigured skips, unavailable retries,
       a bad verdict fails the stage.

    Reads DocumentContent, never the original upload - every downstream
    AI stage works from the same internal representation.
    """
    messages = []
    metadata = {}
    ran = False

    detection = _detect_type(context)
    if detection is not None:
        ran = True
        messages.append(detection["message"])
        metadata["document_type"] = detection["metadata"]

    try:
        result = classify(
            context.content.id,
            processing_job=context.processing_job,
        )
    except ClassifierNotConfigured as exc:
        # Not an error: no layout model is wired up. Skipping is the honest
        # outcome - inventing a label would be worse than having none.
        if not ran:
            return StageResult(message=str(exc), skipped=True)
    except ClassifierUnavailable as exc:
        raise TransientError(str(exc)) from exc
    except ClassificationError as exc:
        raise StageError(str(exc), code=ErrorCode.CLASSIFICATION_FAILED) from exc
    else:
        messages.append(
            f"Layout class {result.predicted_label} ({result.confidence:.2f}) "
            f"via {result.model_name} {result.model_version}"
        )
        # Flat, as before the type detector existed, for anything that reads
        # the layout verdict off this event.
        metadata.update(
            {
                "predicted_label": result.predicted_label,
                "confidence": result.confidence,
                "model_name": result.model_name,
                "model_version": result.model_version,
                "classification_result_id": result.id,
            }
        )

    return StageResult(message="; ".join(messages), metadata=metadata)


def _detect_type(context):
    """Run type detection; None when it is switched off.

    Deliberately catches everything but configuration: the detector is on by
    default for every upload, so a defect in it must cost a document its
    automatic type - not its whole processing run.
    """
    try:
        result, outcome = detect_document_type(
            context.content.id, processing_job=context.processing_job
        )
    except ClassifierNotConfigured:
        return None
    except Exception as exc:
        logger.exception(
            "pipeline.type_detection_failed",
            extra={"content_id": context.content.id},
        )
        return {
            "message": "Type detection failed; the document keeps its type",
            "metadata": {"error": type(exc).__name__},
        }

    return {
        "message": _describe(result, outcome, context),
        "metadata": {
            "label": result.predicted_label,
            "confidence": result.confidence,
            "abstained": result.abstained,
            "top_label": result.metadata.get("top_label"),
            "outcome": outcome,
            "model_name": result.model_name,
            "model_version": result.model_version,
            "classification_result_id": result.id,
        },
    }


def _type_label(value) -> str:
    try:
        return Document.DocumentType(value).label
    except ValueError:
        return str(value)


def _describe(result, outcome, context) -> str:
    """One sentence for the processing timeline, in a reader's words."""
    if result.abstained:
        guess = result.metadata.get("top_label")
        threshold = result.metadata.get("threshold")
        text = "Type not detected with confidence"
        if guess and threshold is not None:
            text += (
                f" (closest: {_type_label(guess)} at {result.confidence:.0%}, "
                f"below {threshold:.0%})"
            )
        if outcome == TypeOutcome.REVERTED:
            text += "; the automatically detected type was reset to General"
        return text

    detected = (
        f"Detected type {_type_label(result.predicted_label)} ({result.confidence:.0%})"
    )
    if outcome == TypeOutcome.APPLIED:
        return f"{detected}, set automatically"
    if outcome == TypeOutcome.USER_CHOICE_KEPT:
        current = (
            Document.objects.filter(pk=context.document_version.document_id)
            .values_list("document_type", flat=True)
            .first()
        )
        return f"{detected}; kept the type chosen by a person ({_type_label(current)})"
    if outcome == TypeOutcome.SUGGESTION_ONLY:
        return f"{detected}, recorded as a suggestion"
    if outcome == TypeOutcome.SUPERSEDED:
        return f"{detected}; not applied, a newer version exists"
    return detected
