from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Reserved label for "the model does not recognise this". A college
# timetable handed to a model trained on invoices and resumes must be
# allowed to come back unknown rather than forced into a known class.
UNKNOWN_LABEL = "unknown"


@dataclass(frozen=True)
class ClassificationPrediction:
    label: str
    confidence: float
    model_name: str
    model_version: str
    input_reference: str

    @property
    def is_unknown(self) -> bool:
        return self.label == UNKNOWN_LABEL


@runtime_checkable
class DocumentClassifier(Protocol):
    """The whole contract between the pipeline and any classifier.

    Implementations may be a linear model, a CNN, a document transformer
    or a remote inference service - the pipeline is not allowed to care.
    """

    name: str
    version: str

    def predict(
        self,
        *,
        document_content_id: int,
    ) -> ClassificationPrediction: ...
