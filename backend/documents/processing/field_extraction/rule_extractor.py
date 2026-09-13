"""A deterministic, offline field extractor built on committed regex packs.

First implementation on purpose: no LLM provider is wired up in this project
and no API keys exist, so the alternative to something deterministic is
nothing at all. It is also the honest baseline - every later extractor has to
beat a rule set that costs nothing to run and never hallucinates.

Confidence is NOT a calibrated probability here and does not pretend to be.
A regex either matched or it did not, so FOUND is reported at 1.0 and
everything else at 0.0, with the reasoning recorded in metadata. A model that
can produce a real probability is free to; the contract permits it.
"""

from pathlib import Path

from django.conf import settings

from .base import ExtractedValue, ExtractionPrediction
from .exceptions import ExtractorUnavailable, NoSchemaForDocumentType
from .matching import FOUND, find_fields
from .schemas import SchemaError, load_pack

DEFAULT_PACK = (
    Path(__file__).resolve().parent / "packs" / "institutional-v1.json"
)


class RuleFieldExtractor:
    """Matches a committed schema pack against normalized page text."""

    name = "rule-field-extractor"
    version = "0.1.0"

    def __init__(self, pack_path=None):
        path = pack_path or getattr(
            settings, "DOCUMENT_FIELD_SCHEMA_PACK", ""
        ) or DEFAULT_PACK
        try:
            self.pack = load_pack(path)
        except SchemaError as exc:
            # Wired up but unusable. Distinct from "not configured", and the
            # message names the pack so the fix is obvious.
            raise ExtractorUnavailable(f"Could not load schema pack: {exc}") from exc

    def extract(self, *, document_content_id, document_type, pages):
        schema = self.pack.for_document_type(document_type)
        if schema is None:
            raise NoSchemaForDocumentType(
                f"Schema pack {self.pack.name} {self.pack.version} has no "
                f"schema for document type {document_type!r}."
            )

        matches = find_fields(schema, pages)

        values = []
        for match in matches:
            span = match.primary
            values.append(
                ExtractedValue(
                    field_name=match.field_name,
                    status=match.status,
                    value=match.value,
                    confidence=1.0 if match.status == FOUND else 0.0,
                    page_number=span.page_number if span else None,
                    character_start=span.character_start if span else None,
                    character_end=span.character_end if span else None,
                    matched_text=span.text if span else "",
                    metadata=match.metadata,
                )
            )

        return ExtractionPrediction(
            values=tuple(values),
            model_name=self.name,
            model_version=self.version,
            schema_name=self.pack.name,
            schema_version=self.pack.version,
            document_type=document_type,
            input_reference=f"document_content:{document_content_id}",
            metadata={
                "pages": len(pages),
                "fields_declared": len(schema.fields),
            },
        )
