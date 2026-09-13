"""The matching core: pure functions over (page_number, text) pairs.

Deliberately knows nothing about Django, the ORM or this project's models.
That split is what lets the same code be scored offline against an evaluation
set with no database at all, and it keeps the rules that decide what a field
*is* testable without building a Document first.
"""

from dataclasses import dataclass, field

from .schemas import VALUE_GROUP, DocumentSchema, FieldRule


@dataclass(frozen=True)
class Span:
    """One located occurrence of a value."""

    page_number: int
    character_start: int
    character_end: int
    text: str

    @property
    def length(self) -> int:
        return self.character_end - self.character_start


@dataclass
class FieldMatch:
    """What matching concluded about one schema field.

    `status` deliberately mirrors the stored ExtractedField.Status values, so
    the ORM adapter is a transcription rather than a translation.
    """

    field_name: str
    status: str
    value: str = ""
    spans: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def primary(self):
        return self.spans[0] if self.spans else None


FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND"
INVALID = "INVALID"
CONFLICTED = "CONFLICTED"


def normalize_value(text: str) -> str:
    """Collapse whitespace only.

    Deliberately conservative: regulation numbers, course codes, dates and
    amounts are the signal here, so case, punctuation and digits are left
    exactly as the document had them.
    """
    return " ".join(text.split())


def _validate(rule: FieldRule, value: str) -> str:
    """Returns a failure reason, or "" when the value is acceptable."""
    if not value:
        return "empty after normalization"
    if rule.max_length and len(value) > rule.max_length:
        return f"{len(value)} characters, over the {rule.max_length} limit"
    if rule.compiled_value and not rule.compiled_value.fullmatch(value):
        return f"does not match value_pattern {rule.value_pattern!r}"
    return ""


def find_field(rule: FieldRule, pages) -> FieldMatch:
    """Locate one field across pages.

    Every pattern is tried on every page rather than stopping at the first
    hit, because stopping early would hide the case this design cares most
    about: the same field appearing twice with different values. A silent
    first-wins pick is exactly the kind of confident wrong answer this
    project refuses to produce.
    """
    spans_by_value = {}
    # Two patterns in the same rule can match the same text - an alternate
    # spelling of the same heading, say. Without this, one occurrence in the
    # document would be counted as several.
    seen_spans = set()

    for page_number, text in pages:
        for regex in rule.compiled:
            for match in regex.finditer(text):
                raw = match.group(VALUE_GROUP)
                if raw is None:
                    # The group is inside an optional branch that did not
                    # participate. There is nothing to locate, so there is
                    # nothing to report - and span() would be (-1, -1).
                    continue

                start, end = match.span(VALUE_GROUP)
                key = (page_number, start, end)
                if key in seen_spans:
                    continue
                seen_spans.add(key)

                value = normalize_value(raw)
                if not value:
                    continue

                spans_by_value.setdefault(value, []).append(
                    Span(
                        page_number=page_number,
                        character_start=start,
                        character_end=end,
                        text=raw,
                    )
                )

    if not spans_by_value:
        return FieldMatch(
            field_name=rule.name,
            status=NOT_FOUND,
            metadata={"patterns_tried": len(rule.compiled)},
        )

    if len(spans_by_value) > 1:
        # Disagreement is reported, never resolved by guessing. Every
        # competing value stays on record with where it was seen.
        ranked = sorted(
            spans_by_value.items(), key=lambda item: (-len(item[1]), item[0])
        )
        return FieldMatch(
            field_name=rule.name,
            status=CONFLICTED,
            value="",
            spans=[spans[0] for _, spans in ranked],
            metadata={
                "candidates": [
                    {
                        "value": value,
                        "occurrences": len(spans),
                        "pages": sorted({s.page_number for s in spans}),
                    }
                    for value, spans in ranked
                ]
            },
        )

    value, spans = next(iter(spans_by_value.items()))
    reason = _validate(rule, value)
    if reason:
        return FieldMatch(
            field_name=rule.name,
            status=INVALID,
            value=value,
            spans=spans,
            metadata={"validation_error": reason},
        )

    return FieldMatch(
        field_name=rule.name,
        status=FOUND,
        value=value,
        spans=spans,
        metadata={"occurrences": len(spans)},
    )


def find_fields(schema: DocumentSchema, pages) -> list:
    """Every field the schema declares, in declaration order.

    Fields that were not found are returned too - an omitted field would be
    indistinguishable from one that was never looked for.
    """
    return [find_field(rule, pages) for rule in schema.fields]
