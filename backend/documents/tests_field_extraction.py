"""Field extraction: schemas, matching, the service boundary and the stage.

The matching tests deliberately avoid the database entirely - `find_fields`
is a pure function over (page_number, text) pairs, and keeping its tests
pure is what proves the same code can be scored offline against an
evaluation set later.
"""

from django.test import TestCase, override_settings

from documents.models import (
    ClassificationResult,
    Document,
    DocumentContent,
    ExtractedField,
    ExtractionResult,
    PageContent,
    ProcessingJob,
)
from documents.processing.context import IngestionContext
from documents.processing.errors import ErrorCode, StageError, TransientError
from documents.processing.field_extraction import service
from documents.processing.field_extraction.base import (
    ExtractedValue,
    ExtractionPrediction,
)
from documents.processing.field_extraction.exceptions import (
    ExtractorNotConfigured,
    ExtractorUnavailable,
    FieldExtractionError,
    NoSchemaForDocumentType,
)
from documents.processing.field_extraction.matching import (
    CONFLICTED,
    FOUND,
    INVALID,
    NOT_FOUND,
    find_field,
    find_fields,
    normalize_value,
)
from documents.processing.field_extraction.rule_extractor import (
    DEFAULT_PACK,
    RuleFieldExtractor,
)
from documents.processing.field_extraction.schemas import (
    SchemaError,
    load_pack,
    load_pack_data,
)
from documents.processing.stages import field_extraction as field_extraction_stage
from documents.tests import MEDIA_ROOT, make_user

RULE_EXTRACTOR = (
    "documents.processing.field_extraction.rule_extractor.RuleFieldExtractor"
)


def pack_data(**overrides):
    """A minimal valid pack, so each test can break exactly one thing."""
    data = {
        "name": "test-pack",
        "version": "1.0.0",
        "schemas": {
            "CIRCULAR": [
                {
                    "name": "circular_number",
                    "patterns": [
                        r"circular\s*no\.?\s*[:\-]?\s*(?P<value>[A-Z0-9/\-]+)"
                    ],
                }
            ]
        },
    }
    data.update(overrides)
    return data


def one_rule(pattern, **extra):
    data = pack_data(
        schemas={"CIRCULAR": [{"name": "f", "patterns": [pattern], **extra}]}
    )
    return load_pack_data(data).for_document_type("CIRCULAR").fields[0]


# --- Schema loading ---------------------------------------------------------


class SchemaLoadingTests(TestCase):
    def test_loads_a_valid_pack(self):
        pack = load_pack_data(pack_data())
        self.assertEqual(pack.name, "test-pack")
        self.assertEqual(pack.version, "1.0.0")
        self.assertEqual(
            pack.for_document_type("CIRCULAR").field_names, ("circular_number",)
        )

    def test_unknown_document_type_is_none_not_an_error(self):
        """A pack covering circulars says nothing about syllabi."""
        self.assertIsNone(load_pack_data(pack_data()).for_document_type("SYLLABUS"))

    def test_rejects_pattern_without_a_value_group(self):
        with self.assertRaises(SchemaError) as caught:
            one_rule(r"circular no\. \d+")
        self.assertIn("value", str(caught.exception))

    def test_rejects_invalid_regex(self):
        with self.assertRaises(SchemaError):
            one_rule(r"(?P<value>[unclosed")

    def test_rejects_field_with_no_patterns(self):
        with self.assertRaises(SchemaError):
            load_pack_data(
                pack_data(schemas={"CIRCULAR": [{"name": "f", "patterns": []}]})
            )

    def test_rejects_duplicate_field_names(self):
        with self.assertRaises(SchemaError) as caught:
            load_pack_data(
                pack_data(
                    schemas={
                        "CIRCULAR": [
                            {"name": "f", "patterns": [r"(?P<value>a)"]},
                            {"name": "f", "patterns": [r"(?P<value>b)"]},
                        ]
                    }
                )
            )
        self.assertIn("duplicate", str(caught.exception).lower())

    def test_rejects_pack_without_name_or_version(self):
        with self.assertRaises(SchemaError):
            load_pack_data({"schemas": {"CIRCULAR": []}})

    def test_rejects_pack_with_no_schemas(self):
        with self.assertRaises(SchemaError):
            load_pack_data(pack_data(schemas={}))

    def test_rejects_missing_file(self):
        with self.assertRaises(SchemaError):
            load_pack("/nonexistent/pack.json")

    def test_shipped_packs_compile(self):
        """The committed packs are configuration, so they are tested."""
        institutional = load_pack(DEFAULT_PACK)
        self.assertEqual(institutional.name, "institutional")
        self.assertIn("CIRCULAR", institutional.schemas)

        invoice = load_pack(DEFAULT_PACK.parent / "invoice-v1.json")
        self.assertEqual(
            invoice.for_document_type("OTHER").field_names,
            ("company", "invoice_number", "invoice_date", "total", "currency"),
        )


# --- Matching (pure, no database) -------------------------------------------


class MatchingTests(TestCase):
    def test_normalize_collapses_whitespace_only(self):
        self.assertEqual(normalize_value("  R20   \n Regs "), "R20 Regs")

    def test_normalize_keeps_case_digits_and_punctuation(self):
        """Course codes and reference numbers are the signal here."""
        self.assertEqual(normalize_value("CS-101/A.2"), "CS-101/A.2")

    def test_finds_a_value_and_reports_its_span(self):
        rule = one_rule(r"no\.\s*(?P<value>\d+)")
        text = "Circular no. 4471 issued today"
        match = find_field(rule, [(1, text)])

        self.assertEqual(match.status, FOUND)
        self.assertEqual(match.value, "4471")
        span = match.primary
        self.assertEqual(span.page_number, 1)
        self.assertEqual(text[span.character_start : span.character_end], "4471")

    def test_span_points_at_the_value_not_the_whole_match(self):
        rule = one_rule(r"ref\s*:\s*(?P<value>[A-Z]+\d+)")
        text = "ref: AB123"
        span = find_field(rule, [(1, text)]).primary
        self.assertEqual(text[span.character_start : span.character_end], "AB123")

    def test_absent_field_is_not_found_not_omitted(self):
        rule = one_rule(r"no\.\s*(?P<value>\d+)")
        match = find_field(rule, [(1, "nothing relevant here")])
        self.assertEqual(match.status, NOT_FOUND)
        self.assertEqual(match.value, "")

    def test_same_value_twice_is_found_once_with_both_spans(self):
        rule = one_rule(r"no\.\s*(?P<value>\d+)")
        match = find_field(rule, [(1, "no. 12"), (2, "no. 12")])
        self.assertEqual(match.status, FOUND)
        self.assertEqual(match.value, "12")
        self.assertEqual([s.page_number for s in match.spans], [1, 2])

    def test_disagreeing_values_conflict_rather_than_first_wins(self):
        """A silent first-wins pick is exactly the confident wrong answer
        this project refuses to produce."""
        rule = one_rule(r"no\.\s*(?P<value>\d+)")
        match = find_field(rule, [(1, "no. 12"), (2, "no. 99")])

        self.assertEqual(match.status, CONFLICTED)
        self.assertEqual(match.value, "")
        self.assertEqual(
            sorted(c["value"] for c in match.metadata["candidates"]), ["12", "99"]
        )

    def test_value_failing_value_pattern_is_invalid_not_dropped(self):
        rule = one_rule(r"credits?\s*:\s*(?P<value>\S+)", value_pattern=r"\d{1,2}")
        match = find_field(rule, [(1, "Credits: many")])

        self.assertEqual(match.status, INVALID)
        self.assertEqual(match.value, "many")
        self.assertIn("value_pattern", match.metadata["validation_error"])

    def test_value_over_max_length_is_invalid(self):
        rule = one_rule(r"sub\s*:\s*(?P<value>.+)", max_length=5)
        match = find_field(rule, [(1, "Sub: a very long subject line")])
        self.assertEqual(match.status, INVALID)

    def test_every_declared_field_gets_a_result(self):
        """Recall needs a denominator."""
        pack = load_pack_data(
            pack_data(
                schemas={
                    "CIRCULAR": [
                        {"name": "a", "patterns": [r"a=(?P<value>\d+)"]},
                        {"name": "b", "patterns": [r"b=(?P<value>\d+)"]},
                    ]
                }
            )
        )
        matches = find_fields(pack.for_document_type("CIRCULAR"), [(1, "a=1")])
        self.assertEqual([m.field_name for m in matches], ["a", "b"])
        self.assertEqual([m.status for m in matches], [FOUND, NOT_FOUND])

    def test_alternate_patterns_are_all_tried(self):
        pack = load_pack_data(
            pack_data(
                schemas={
                    "CIRCULAR": [
                        {
                            "name": "f",
                            "patterns": [
                                r"first=(?P<value>\d+)",
                                r"second=(?P<value>\d+)",
                            ],
                        }
                    ]
                }
            )
        )
        rule = pack.for_document_type("CIRCULAR").fields[0]
        self.assertEqual(find_field(rule, [(1, "second=7")]).value, "7")


# --- The rule extractor -----------------------------------------------------


class RuleExtractorTests(TestCase):
    def test_extracts_the_institutional_circular_fields(self):
        extractor = RuleFieldExtractor()
        pages = [
            (
                1,
                "OFFICE OF THE REGISTRAR\n"
                "Circular No: REG/2024/117\n"
                "Date: 14/03/2024\n"
                "Sub: Revised examination timetable\n"
                "Issued by: Controller of Examinations\n",
            )
        ]
        prediction = extractor.extract(
            document_content_id=1, document_type="CIRCULAR", pages=pages
        )
        found = {
            v.field_name: v.value
            for v in prediction.values
            if v.status == ExtractedField.Status.FOUND
        }

        self.assertEqual(found["circular_number"], "REG/2024/117")
        self.assertEqual(found["issue_date"], "14/03/2024")
        self.assertEqual(found["subject"], "Revised examination timetable")

    def test_body_prose_does_not_trigger_issuing_authority(self):
        """A regression from a live run: the loose `from` alternative also
        matched "scheduled from 02/04/2024", turning a correctly-found
        authority into a spurious CONFLICTED. The rule now requires the
        keyword at the start of a line and a real separator."""
        pages = [
            (
                1,
                "Circular No: REG/2024/117\n"
                "Issued by: Controller of Examinations\n"
                "\n"
                "Examinations scheduled from 02/04/2024 stand revised.\n",
            )
        ]
        prediction = RuleFieldExtractor().extract(
            document_content_id=1, document_type="CIRCULAR", pages=pages
        )
        authority = next(
            v for v in prediction.values if v.field_name == "issuing_authority"
        )
        self.assertEqual(authority.status, ExtractedField.Status.FOUND)
        self.assertEqual(authority.value, "Controller of Examinations")

    def test_reports_the_schema_it_used(self):
        prediction = RuleFieldExtractor().extract(
            document_content_id=7, document_type="CIRCULAR", pages=[(1, "")]
        )
        self.assertEqual(prediction.schema_name, "institutional")
        self.assertEqual(prediction.schema_version, "1.0.0")
        self.assertEqual(prediction.input_reference, "document_content:7")

    def test_document_type_without_a_schema_raises_no_schema(self):
        with self.assertRaises(NoSchemaForDocumentType):
            RuleFieldExtractor().extract(
                document_content_id=1, document_type="GENERAL", pages=[(1, "x")]
            )

    def test_unreadable_pack_is_unavailable_not_a_crash(self):
        with self.assertRaises(ExtractorUnavailable):
            RuleFieldExtractor(pack_path="/nonexistent/pack.json")

    def test_invoice_pack_satisfies_the_spec_example_without_code_change(self):
        """Section 11.2's worked example, proved as data rather than code."""
        extractor = RuleFieldExtractor(
            pack_path=DEFAULT_PACK.parent / "invoice-v1.json"
        )
        pages = [
            (
                1,
                "From: Acme Supplies Ltd\n"
                "Invoice No: INV-2024-88\n"
                "Date: 02/04/2024\n"
                "Currency: USD\n"
                "Total: 1,240.50\n",
            )
        ]
        prediction = extractor.extract(
            document_content_id=1, document_type="OTHER", pages=pages
        )
        found = {
            v.field_name: v.value
            for v in prediction.values
            if v.status == ExtractedField.Status.FOUND
        }
        self.assertEqual(found["invoice_number"], "INV-2024-88")
        self.assertEqual(found["total"], "1,240.50")
        self.assertEqual(found["currency"], "USD")

    def test_confidence_is_not_dressed_up_as_a_probability(self):
        """A regex matched or it did not; 1.0/0.0 says exactly that."""
        prediction = RuleFieldExtractor().extract(
            document_content_id=1,
            document_type="CIRCULAR",
            pages=[(1, "Circular No: X/1")],
        )
        for value in prediction.values:
            expected = 1.0 if value.status == ExtractedField.Status.FOUND else 0.0
            self.assertEqual(value.confidence, expected)


# --- The service boundary ---------------------------------------------------


class StubExtractor:
    """Returns whatever a test hands it, to exercise the boundary checks."""

    name = "stub"
    version = "0.0.1"
    values = ()

    def extract(self, *, document_content_id, document_type, pages):
        return ExtractionPrediction(
            values=type(self).values,
            model_name=self.name,
            model_version=self.version,
            schema_name="stub-schema",
            schema_version="1.0.0",
            document_type=document_type,
            input_reference=f"document_content:{document_content_id}",
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ServiceBoundaryTests(TestCase):
    PAGE_TEXT = "Circular No: REG/2024/117 issued by the Registrar"

    def setUp(self):
        service.reset_extractor_cache()
        self.addCleanup(service.reset_extractor_cache)

        user = make_user("extractor_user")
        document = Document.objects.create(
            title="Circular", document_type="CIRCULAR", owner=user
        )
        version = document.versions.create(
            version_number=1, file_name="c.txt", file_size=1, uploaded_by=user
        )
        self.job = ProcessingJob.objects.create(document_version=version)
        self.content = DocumentContent.objects.create(
            document_version=version,
            processing_job=self.job,
            processing_version="t",
            page_count=1,
        )
        PageContent.objects.create(
            content=self.content,
            page_number=1,
            text=self.PAGE_TEXT,
            extraction_method=PageContent.ExtractionMethod.PLAIN_TEXT,
            character_count=len(self.PAGE_TEXT),
        )

    def run_with(self, values):
        StubExtractor.values = tuple(values)
        with override_settings(DOCUMENT_FIELD_EXTRACTOR=f"{__name__}.StubExtractor"):
            return service.extract(self.content.id, processing_job=self.job)

    def good_value(self, **overrides):
        start = self.PAGE_TEXT.index("REG/2024/117")
        data = {
            "field_name": "circular_number",
            "status": "FOUND",
            "value": "REG/2024/117",
            "confidence": 1.0,
            "page_number": 1,
            "character_start": start,
            "character_end": start + len("REG/2024/117"),
            "matched_text": "REG/2024/117",
        }
        data.update(overrides)
        return ExtractedValue(**data)

    def test_unconfigured_extractor_raises_not_configured(self):
        with override_settings(DOCUMENT_FIELD_EXTRACTOR=""):
            with self.assertRaises(ExtractorNotConfigured):
                service.extract(self.content.id)

    def test_persists_fields_with_their_spans(self):
        result = self.run_with([self.good_value()])

        self.assertEqual(result.field_count, 1)
        self.assertEqual(result.found_count, 1)
        field = result.extracted_fields.get()
        self.assertEqual(field.value, "REG/2024/117")
        self.assertEqual(
            self.PAGE_TEXT[field.character_start : field.character_end],
            field.matched_text,
        )

    def test_counts_only_found_fields(self):
        result = self.run_with(
            [self.good_value(), ExtractedValue(field_name="other", status="NOT_FOUND")]
        )
        self.assertEqual(result.field_count, 2)
        self.assertEqual(result.found_count, 1)

    def test_rejects_a_span_that_does_not_re_slice_to_the_claimed_text(self):
        """The invariant: a value that cannot be pointed at is not evidence."""
        with self.assertRaises(FieldExtractionError) as caught:
            self.run_with([self.good_value(character_start=0, character_end=4)])
        self.assertIn("claims", str(caught.exception))
        self.assertFalse(ExtractionResult.objects.exists())

    def test_rejects_a_span_past_the_end_of_the_page(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(character_end=10_000)])

    def test_rejects_a_span_on_a_page_the_document_does_not_have(self):
        with self.assertRaises(FieldExtractionError) as caught:
            self.run_with([self.good_value(page_number=9)])
        self.assertIn("does not have", str(caught.exception))

    def test_rejects_inverted_span(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(character_start=20, character_end=5)])

    def test_rejects_found_without_a_span(self):
        with self.assertRaises(FieldExtractionError) as caught:
            self.run_with(
                [
                    ExtractedValue(
                        field_name="f", status="FOUND", value="x", confidence=1.0
                    )
                ]
            )
        self.assertIn("without a span", str(caught.exception))

    def test_rejects_found_with_an_empty_value(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(value="")])

    def test_rejects_confidence_outside_zero_to_one(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(confidence=1.4)])

    def test_rejects_unknown_status(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(status="PROBABLY")])

    def test_rejects_duplicate_field_names(self):
        with self.assertRaises(FieldExtractionError) as caught:
            self.run_with([self.good_value(), self.good_value()])
        self.assertIn("more than once", str(caught.exception))

    def test_nothing_is_persisted_when_validation_fails(self):
        with self.assertRaises(FieldExtractionError):
            self.run_with([self.good_value(page_number=9)])
        self.assertFalse(ExtractionResult.objects.exists())
        self.assertFalse(ExtractedField.objects.exists())

    def test_reprocessing_appends_a_result_rather_than_overwriting(self):
        first = self.run_with([self.good_value()])
        second = self.run_with([self.good_value()])

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(ExtractionResult.objects.count(), 2)
        self.assertEqual(
            list(ExtractionResult.objects.order_by("id").values_list("id", flat=True)),
            [first.id, second.id],
        )


# --- The pipeline stage -----------------------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class FieldExtractionStageTests(TestCase):
    def setUp(self):
        service.reset_extractor_cache()
        self.addCleanup(service.reset_extractor_cache)

        user = make_user("stage_user")
        self.document = Document.objects.create(
            title="Circular", document_type="CIRCULAR", owner=user
        )
        version = self.document.versions.create(
            version_number=1, file_name="c.txt", file_size=1, uploaded_by=user
        )
        self.job = ProcessingJob.objects.create(document_version=version)
        self.content = DocumentContent.objects.create(
            document_version=version,
            processing_job=self.job,
            processing_version="t",
            page_count=1,
        )
        PageContent.objects.create(
            content=self.content,
            page_number=1,
            text="Circular No: REG/2024/117\nDate: 14/03/2024\n",
            extraction_method=PageContent.ExtractionMethod.PLAIN_TEXT,
            character_count=42,
        )
        self.context = IngestionContext(
            document_version=version, file_path="", processing_job=self.job
        )
        self.context.content = self.content

    def test_skips_when_no_extractor_is_configured(self):
        with override_settings(DOCUMENT_FIELD_EXTRACTOR=""):
            result = field_extraction_stage.run(self.context)

        self.assertTrue(result.skipped)
        self.assertFalse(ExtractionResult.objects.exists())

    def test_skips_when_the_pack_has_no_schema_for_the_document_type(self):
        self.document.document_type = "GENERAL"
        self.document.save(update_fields=["document_type"])

        with override_settings(DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR):
            result = field_extraction_stage.run(self.context)

        self.assertTrue(result.skipped)
        self.assertIn("GENERAL", result.message)

    def test_extracts_and_reports_counts(self):
        with override_settings(DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR):
            result = field_extraction_stage.run(self.context)

        self.assertFalse(result.skipped)
        self.assertEqual(result.metadata["schema_name"], "institutional")
        self.assertEqual(result.metadata["found_count"], 2)
        self.assertEqual(result.metadata["field_count"], 4)

        stored = ExtractionResult.objects.get()
        self.assertEqual(stored.processing_job_id, self.job.id)
        self.assertEqual(
            stored.extracted_fields.get(field_name="circular_number").value,
            "REG/2024/117",
        )

    def test_unavailable_extractor_is_transient(self):
        with override_settings(
            DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR,
            DOCUMENT_FIELD_SCHEMA_PACK="/nonexistent/pack.json",
        ):
            with self.assertRaises(TransientError) as caught:
                field_extraction_stage.run(self.context)
        self.assertTrue(caught.exception.retryable)

    def test_extraction_failure_is_permanent_with_its_own_error_code(self):
        StubExtractor.values = (
            ExtractedValue(field_name="f", status="FOUND", value="x", confidence=1.0),
        )
        with override_settings(DOCUMENT_FIELD_EXTRACTOR=f"{__name__}.StubExtractor"):
            with self.assertRaises(StageError) as caught:
                field_extraction_stage.run(self.context)

        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.code, ErrorCode.FIELD_EXTRACTION_FAILED)

    def test_does_not_depend_on_classification(self):
        """Extraction picks its schema from document_type, never from a
        classifier verdict - DOCUMENT_CLASSIFIER is empty by default, so a
        dependency would make extraction silently vary."""
        self.assertFalse(ClassificationResult.objects.exists())

        with override_settings(DOCUMENT_FIELD_EXTRACTOR=RULE_EXTRACTOR):
            result = field_extraction_stage.run(self.context)

        self.assertFalse(result.skipped)
        self.assertEqual(result.metadata["document_type"], "CIRCULAR")
