from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExtractedValue:
    """One field's outcome, as an extractor reports it.

    Mirrors the stored ExtractedField so the service persists rather than
    translates. Offsets are optional because NOT_FOUND has no span to point
    at, and 0 is a real offset - None is the only honest "nowhere".
    """

    field_name: str
    status: str
    value: str = ""
    confidence: float = 0.0
    page_number: int = None
    character_start: int = None
    character_end: int = None
    matched_text: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def has_span(self) -> bool:
        return (
            self.page_number is not None
            and self.character_start is not None
            and self.character_end is not None
        )


@dataclass(frozen=True)
class ExtractionPrediction:
    """Everything one extraction pass produced."""

    values: tuple
    model_name: str
    model_version: str
    schema_name: str
    schema_version: str
    document_type: str
    input_reference: str
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class FieldExtractor(Protocol):
    """The whole contract between the pipeline and any field extractor.

    Implementations may be a regex pack, a document-AI model or a remote
    service - the pipeline is not allowed to care. An implementation is
    handed the already-normalized pages rather than a file path, so it
    cannot quietly reach past DocumentContent to the original upload.
    """

    name: str
    version: str

    def extract(
        self,
        *,
        document_content_id: int,
        document_type: str,
        pages: list,
    ) -> ExtractionPrediction: ...
