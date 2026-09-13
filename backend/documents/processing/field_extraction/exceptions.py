class FieldExtractionError(Exception):
    """Base for field-extraction failures."""


class ExtractorNotConfigured(FieldExtractionError):
    """No field extractor is wired up.

    Raised rather than silently returning an empty field set: "nothing was
    configured" and "the extractor ran and found nothing" are different
    facts, and only one of them says anything about the document.
    """


class NoSchemaForDocumentType(FieldExtractionError):
    """An extractor is configured, but the pack has no schema for this type.

    Also not a failure. A pack covering circulars says nothing about
    syllabi, and inventing an empty result would put a misleading row on
    record.
    """


class ExtractorUnavailable(FieldExtractionError):
    """A configured extractor could not be loaded or reached.

    Distinct from ExtractorNotConfigured: something is wired up but is
    failing, which may be worth retrying.
    """
