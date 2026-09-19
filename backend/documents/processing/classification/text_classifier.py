"""Inference adapter for the TF-IDF text baseline trained in `ai/`.

This module is the entire Django-side half of the training handoff. It
loads a model artifact by path and never imports `ai` - the artifact
contains only scikit-learn and builtin objects, which is what makes the
two halves of the project independently movable.

    ai/classification/training/train_text.py
        |
        v  model.joblib  (sklearn objects + labels + threshold + params)
        |
    this adapter  --DOCUMENT_CLASSIFIER-->  pipeline -> ClassificationResult

Two properties are worth stating explicitly because they are easy to
lose:

*Normalization travels with the model.* The artifact records the exact
preprocessing parameters used at training time, and they are applied
here rather than hard-coded, so serving cannot quietly diverge from
training. A parameter this code does not implement is an error, not
something to skip.

*The model was trained on single pages.* RVL-CDIP is a page-image
corpus, so a page is what it has ever seen. A multi-page PDF is
therefore scored page by page and the probabilities averaged, keeping
every call to the model in-distribution instead of feeding it a
concatenation of a shape it was never fitted on.
"""

import unicodedata
from pathlib import Path

from django.conf import settings

from documents.models import DocumentContent

from .base import UNKNOWN_LABEL, ClassificationPrediction
from .exceptions import ClassificationError, ClassifierUnavailable

SUPPORTED_PREPROCESSING = frozenset(
    {"unicode_normalization", "lowercase", "collapse_whitespace"}
)

UNICODE_FORMS = ("NFC", "NFD", "NFKC", "NFKD")


def normalize(text: str, parameters: dict) -> str:
    """Mirror of ai.classification.preprocessing.text.apply.

    Deliberately duplicated rather than imported: importing it would
    couple the Django process to the training package and defeat the
    boundary. The duplication is safe only because the transformation is
    parameterised by the artifact and both sides refuse parameters they
    do not implement - see the equivalence test in tests_classification.
    """
    unknown = set(parameters) - SUPPORTED_PREPROCESSING
    if unknown:
        raise ClassificationError(
            f"Artifact requires preprocessing this adapter does not "
            f"implement: {sorted(unknown)}. Serving would not match training."
        )

    form = parameters.get("unicode_normalization", "NFKC")
    if form:
        if form not in UNICODE_FORMS:
            raise ClassificationError(f"Unknown unicode normalization {form!r}.")
        text = unicodedata.normalize(form, text)

    if parameters.get("lowercase", True):
        text = text.lower()

    if parameters.get("collapse_whitespace", True):
        text = " ".join(text.split())

    return text


class TextDocumentClassifier:
    """Classifies a DocumentContent using the trained text baseline.

    Instantiated once per process by the classification service, since
    loading the artifact on every document would dominate inference cost.
    """

    def __init__(self, artifact_path: str | None = None):
        # Checked before Path(), not after: Path("") is PosixPath("."), whose
        # str() is "." and therefore truthy, so an emptiness test on the Path
        # never fires and an unset setting would be reported as the baffling
        # "No model artifact at .".
        configured = artifact_path or getattr(
            settings, "DOCUMENT_CLASSIFIER_ARTIFACT", ""
        )

        if not configured:
            raise ClassifierUnavailable(
                "DOCUMENT_CLASSIFIER_ARTIFACT is not set, but a classifier "
                "is configured. Point it at a trained model.joblib."
            )

        path = Path(configured)

        if not path.is_file():
            raise ClassifierUnavailable(
                f"No model artifact at {path}. Train one first:\n"
                f"  python -m ai.classification.training.train_text"
            )

        try:
            import joblib
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise ClassifierUnavailable(
                "scikit-learn and joblib are required to run the text "
                "classifier: pip install -r backend/requirements-ml.txt"
            ) from exc

        try:
            artifact = joblib.load(path)
        except Exception as exc:
            raise ClassifierUnavailable(
                f"Could not load model artifact {path}: {exc}"
            ) from exc

        try:
            self._model = artifact["model"]
            self.name = artifact["model_name"]
            self.version = artifact["model_version"]
            self._threshold = float(artifact["threshold"])
            self._preprocessing = artifact["preprocessing"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ClassifierUnavailable(
                f"Model artifact {path} is missing expected fields: {exc}"
            ) from exc

        # Probability columns are ordered by the fitted estimator's own
        # classes_, which sklearn sorts alphabetically. Reading the order
        # from anywhere else silently mislabels every prediction.
        self._classes = list(self._model.classes_)
        self._artifact_path = str(path)

    def predict(self, *, document_content_id: int) -> ClassificationPrediction:
        pages = list(
            DocumentContent.objects.get(id=document_content_id)
            .pages.order_by("page_number")
            .values_list("page_number", "text")
        )

        scored = [
            (number, normalize(text, self._preprocessing)) for number, text in pages
        ]
        usable = [(number, text) for number, text in scored if text]

        if not usable:
            # Nothing to read. Abstaining is the only honest answer - the
            # pipeline's OCR stage normally catches this first.
            return self._prediction(
                label=UNKNOWN_LABEL,
                confidence=0.0,
                content_id=document_content_id,
                metadata={
                    "reason": "no_text",
                    "pages_total": len(pages),
                    "pages_scored": 0,
                },
            )

        probabilities = self._model.predict_proba([text for _, text in usable])

        # Mean over pages: every page gets an equal vote, so a long
        # document is not decided by whichever page happens to be first.
        averaged = [
            sum(row[i] for row in probabilities) / len(probabilities)
            for i in range(len(self._classes))
        ]
        best = max(range(len(averaged)), key=averaged.__getitem__)
        confidence = float(averaged[best])
        label = self._classes[best]

        abstained = confidence < self._threshold

        return self._prediction(
            label=UNKNOWN_LABEL if abstained else label,
            confidence=confidence,
            content_id=document_content_id,
            metadata={
                "pages_total": len(pages),
                "pages_scored": len(usable),
                "scored_page_numbers": [number for number, _ in usable],
                "threshold": self._threshold,
                "abstained": abstained,
                # What it would have said, so a threshold can be retuned
                # later against real documents instead of guessed at.
                "top_label": label,
                "scores": {
                    name: round(float(score), 6)
                    # strict=True: classes and scores come from the same
                    # artifact and must agree; if they do not, a label is
                    # being reported against another class's score.
                    for name, score in zip(self._classes, averaged, strict=True)
                },
            },
        )

    def _prediction(self, *, label, confidence, content_id, metadata):
        return ClassificationPrediction(
            label=label,
            confidence=confidence,
            model_name=self.name,
            model_version=self.version,
            input_reference=f"document_content:{content_id}",
            metadata={"artifact": self._artifact_path, **metadata},
        )
