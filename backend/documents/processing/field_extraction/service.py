import functools

from django.conf import settings
from django.db import transaction
from django.utils.module_loading import import_string

from documents.models import DocumentContent, ExtractedField, ExtractionResult

from .base import ExtractionPrediction, FieldExtractor
from .exceptions import ExtractorNotConfigured, FieldExtractionError

VALID_STATUSES = {choice.value for choice in ExtractedField.Status}


@functools.cache
def _load_extractor(path: str, pack: str) -> FieldExtractor:
    """Instantiate once per (dotted path, pack) pair.

    Compiling a schema pack on every document would dominate the cost of
    matching it, so instances are cached. `pack` is part of the key rather
    than a constructor argument: the extractor reads the setting itself,
    but keying on the dotted path alone would hand back an instance still
    holding the previous pack after the setting changed.
    """
    return import_string(path)()


def reset_extractor_cache() -> None:
    _load_extractor.cache_clear()


def get_extractor() -> FieldExtractor:
    path = getattr(settings, "DOCUMENT_FIELD_EXTRACTOR", "")

    if not path:
        raise ExtractorNotConfigured(
            "No DOCUMENT_FIELD_EXTRACTOR configured. Field extraction is "
            "skipped rather than guessed."
        )

    return _load_extractor(path, getattr(settings, "DOCUMENT_FIELD_SCHEMA_PACK", ""))


def load_pages(content: DocumentContent) -> list:
    """The (page_number, text) pairs an extractor is allowed to see."""
    return list(
        content.pages.order_by("page_number").values_list("page_number", "text")
    )


def _validate(prediction: ExtractionPrediction, pages: dict) -> None:
    """The boundary between our pipeline and an extractor we did not write.

    The span check is the load-bearing one. An extractor that reports a value
    together with offsets that do not re-slice to that value out of the
    stored page text has either hallucinated the value or lost track of where
    it came from, and either way the row must not be written. It turns
    "never fabricate an output" from a principle into an invariant the
    database cannot violate.
    """
    seen = set()

    for value in prediction.values:
        where = f"field {value.field_name!r}"

        if not value.field_name:
            raise FieldExtractionError("Extractor returned a field with no name.")

        if value.field_name in seen:
            raise FieldExtractionError(
                f"Extractor returned {value.field_name!r} more than once; "
                f"one row per schema field is the contract."
            )
        seen.add(value.field_name)

        if value.status not in VALID_STATUSES:
            raise FieldExtractionError(
                f"{where}: unknown status {value.status!r}. "
                f"Expected one of {sorted(VALID_STATUSES)}."
            )

        if not 0.0 <= value.confidence <= 1.0:
            raise FieldExtractionError(
                f"{where}: confidence {value.confidence!r} is outside [0, 1]."
            )

        if value.status == ExtractedField.Status.FOUND:
            if not value.value:
                raise FieldExtractionError(
                    f"{where}: reported FOUND with an empty value."
                )
            if not value.has_span:
                raise FieldExtractionError(
                    f"{where}: reported FOUND without a span. A value that "
                    f"cannot be pointed at is not evidence."
                )

        if not value.has_span:
            continue

        text = pages.get(value.page_number)
        if text is None:
            raise FieldExtractionError(
                f"{where}: cites page {value.page_number}, which this "
                f"document does not have."
            )

        if value.character_start > value.character_end:
            raise FieldExtractionError(
                f"{where}: span start {value.character_start} is after end "
                f"{value.character_end}."
            )

        if value.character_end > len(text):
            raise FieldExtractionError(
                f"{where}: span ends at {value.character_end} but page "
                f"{value.page_number} is {len(text)} characters."
            )

        actual = text[value.character_start : value.character_end]
        if value.matched_text and actual != value.matched_text:
            raise FieldExtractionError(
                f"{where}: span [{value.character_start}:{value.character_end}] "
                f"on page {value.page_number} holds {actual!r}, but the "
                f"extractor claims {value.matched_text!r}."
            )


def extract(document_content_id: int, *, processing_job=None) -> ExtractionResult:
    """Extract fields from one normalized DocumentContent and persist them.

    Raises ExtractorNotConfigured when nothing is wired up, and
    NoSchemaForDocumentType when the pack does not cover this document's
    type; callers decide whether either is fatal or simply means "not
    applicable here".
    """
    content = DocumentContent.objects.select_related("document_version__document").get(
        id=document_content_id
    )
    document = content.document_version.document

    extractor = get_extractor()
    pages = load_pages(content)

    prediction = extractor.extract(
        document_content_id=document_content_id,
        document_type=document.document_type,
        pages=pages,
    )
    _validate(prediction, dict(pages))

    found = sum(
        1 for value in prediction.values if value.status == ExtractedField.Status.FOUND
    )

    with transaction.atomic():
        result = ExtractionResult.objects.create(
            document_version=content.document_version,
            document_content=content,
            processing_job=processing_job,
            model_name=prediction.model_name,
            model_version=prediction.model_version,
            schema_name=prediction.schema_name,
            schema_version=prediction.schema_version,
            document_type=prediction.document_type,
            field_count=len(prediction.values),
            found_count=found,
            input_reference=prediction.input_reference,
            metadata=prediction.metadata,
        )

        ExtractedField.objects.bulk_create(
            [
                ExtractedField(
                    result=result,
                    field_name=value.field_name,
                    status=value.status,
                    value=value.value,
                    confidence=value.confidence,
                    page_number=value.page_number,
                    character_start=value.character_start,
                    character_end=value.character_end,
                    matched_text=value.matched_text,
                    metadata=value.metadata,
                )
                for value in prediction.values
            ]
        )

    return result
