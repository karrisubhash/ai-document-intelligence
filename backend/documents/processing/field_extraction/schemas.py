"""Field schemas, loaded from committed JSON rather than written in code.

The spec's worked example (section 11.2) is an invoice, but this platform's
document types are institutional - GENERAL, REGULATION, POLICY, SYLLABUS,
CIRCULAR, FORM, OTHER. Adding an INVOICE type to satisfy the example would be
fiction about what the platform ingests. Making the schema *data* satisfies it
honestly instead: an invoice schema is ~20 lines of JSON in a pack, with no
code change, no migration and no new model.

A pack is versioned and committed so that a stored ExtractionResult naming
(schema_name, schema_version) can be traced back to the exact rules that
produced it.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A pattern must declare this group; its span is what gets stored as the
# field's provenance. Requiring it means a rule cannot match a whole
# paragraph and then claim a value that is not locatable inside it.
VALUE_GROUP = "value"

MAX_PATTERN_LENGTH = 500


class SchemaError(Exception):
    """A schema pack is malformed.

    Raised at load time, not match time: a bad pack is a configuration
    mistake and should fail loudly on the first document rather than
    silently extracting nothing from every document.
    """


@dataclass(frozen=True)
class FieldRule:
    name: str
    patterns: tuple
    # Compiled once at load. Kept beside the source patterns so an error
    # message can quote what the operator actually wrote.
    compiled: tuple
    required: bool = False
    # Validation applied to the captured value AFTER matching. A value that
    # matches the pattern but fails this is stored as INVALID, not dropped -
    # "the rule fired but produced nonsense" is a different, and more
    # useful, signal than "nothing was found".
    value_pattern: str = ""
    compiled_value: object = None
    max_length: int = 0
    description: str = ""


@dataclass(frozen=True)
class DocumentSchema:
    document_type: str
    fields: tuple = field(default_factory=tuple)

    @property
    def field_names(self) -> tuple:
        return tuple(rule.name for rule in self.fields)


@dataclass(frozen=True)
class SchemaPack:
    name: str
    version: str
    schemas: dict

    def for_document_type(self, document_type: str):
        """The schema for a document type, or None if the pack has none.

        None is a legitimate answer, not an error: a pack that covers
        circulars says nothing about syllabi, and the stage records that as
        SKIPPED rather than inventing an empty result.
        """
        return self.schemas.get(document_type)


# Search patterns scan raw document text, where a heading may be "Subject:",
# "SUBJECT:" or "subject:", so they are case-insensitive and line-anchored.
SEARCH_FLAGS = re.IGNORECASE | re.MULTILINE

# Value patterns are a *constraint* on what was captured, so they are
# case-SENSITIVE. Compiling them with IGNORECASE would silently defeat the
# reason most of them exist: `[A-Z]{3}` would accept "usd", which is exactly
# the check the author was asking for.
VALUE_FLAGS = 0


def _compile(pattern: str, where: str, flags: int = SEARCH_FLAGS):
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise SchemaError(
            f"{where}: pattern is {len(pattern)} characters, over the "
            f"{MAX_PATTERN_LENGTH} limit."
        )
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise SchemaError(f"{where}: invalid regex {pattern!r}: {exc}") from exc
    return compiled


def _load_field(raw: dict, where: str) -> FieldRule:
    name = raw.get("name", "")
    if not name:
        raise SchemaError(f"{where}: field has no name.")

    where = f"{where}.{name}"
    patterns = raw.get("patterns") or []
    if not patterns:
        raise SchemaError(f"{where}: field declares no patterns.")

    compiled = []
    for index, pattern in enumerate(patterns):
        regex = _compile(pattern, f"{where}[{index}]")
        if VALUE_GROUP not in regex.groupindex:
            raise SchemaError(
                f"{where}[{index}]: pattern must declare a "
                f"(?P<{VALUE_GROUP}>...) group so the matched value has a "
                f"storable span."
            )
        compiled.append(regex)

    value_pattern = raw.get("value_pattern", "")
    compiled_value = (
        _compile(value_pattern, f"{where}.value_pattern", flags=VALUE_FLAGS)
        if value_pattern
        else None
    )

    return FieldRule(
        name=name,
        patterns=tuple(patterns),
        compiled=tuple(compiled),
        required=bool(raw.get("required", False)),
        value_pattern=value_pattern,
        compiled_value=compiled_value,
        max_length=int(raw.get("max_length", 0)),
        description=raw.get("description", ""),
    )


def load_pack(path) -> SchemaPack:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"No schema pack at {path}.") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{path}: not valid JSON: {exc}") from exc

    return load_pack_data(raw, source=str(path))


def load_pack_data(raw: dict, *, source: str = "<dict>") -> SchemaPack:
    name = raw.get("name", "")
    version = raw.get("version", "")
    if not name or not version:
        raise SchemaError(f"{source}: pack must declare 'name' and 'version'.")

    schemas = {}
    for document_type, rules in (raw.get("schemas") or {}).items():
        where = f"{source}:{document_type}"
        if not isinstance(rules, list):
            raise SchemaError(f"{where}: schema must be a list of fields.")

        fields = tuple(_load_field(rule, where) for rule in rules)
        names = [rule.name for rule in fields]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise SchemaError(f"{where}: duplicate field name(s) {sorted(duplicates)}.")

        schemas[document_type] = DocumentSchema(
            document_type=document_type, fields=fields
        )

    if not schemas:
        raise SchemaError(f"{source}: pack declares no schemas.")

    return SchemaPack(name=name, version=version, schemas=schemas)
