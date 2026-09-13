from documents.processing.context import StageResult
from documents.processing.errors import ErrorCode, StageError, TransientError
from documents.processing.field_extraction import extract
from documents.processing.field_extraction.exceptions import (
    ExtractorNotConfigured,
    ExtractorUnavailable,
    FieldExtractionError,
    NoSchemaForDocumentType,
)


def run(context) -> StageResult:
    """Extract configured fields from the normalized content.

    Reads DocumentContent, never the original upload - every downstream AI
    stage works from the same internal representation.
    """
    try:
        result = extract(
            context.content.id,
            processing_job=context.processing_job,
        )
    except ExtractorNotConfigured as exc:
        # Not an error: no extractor is wired up yet. Skipping is the honest
        # outcome - inventing fields would be worse than having none.
        return StageResult(message=str(exc), skipped=True)
    except NoSchemaForDocumentType as exc:
        # Also not an error. The pack legitimately does not cover this type,
        # and the message names it so the event log explains itself.
        return StageResult(message=str(exc), skipped=True)
    except ExtractorUnavailable as exc:
        raise TransientError(str(exc)) from exc
    except FieldExtractionError as exc:
        raise StageError(str(exc), code=ErrorCode.FIELD_EXTRACTION_FAILED) from exc

    return StageResult(
        message=(
            f"{result.found_count}/{result.field_count} field(s) found "
            f"against {result.schema_name} {result.schema_version}"
        ),
        metadata={
            "schema_name": result.schema_name,
            "schema_version": result.schema_version,
            "document_type": result.document_type,
            "field_count": result.field_count,
            "found_count": result.found_count,
            "model_name": result.model_name,
            "model_version": result.model_version,
            "extraction_result_id": result.id,
        },
    )
