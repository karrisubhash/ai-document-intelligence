"""Phase 9: structured logging, redaction, correlation ids and metrics.

The redaction tests are the important ones. Section 13 forbids document
content in ordinary logs, and that is a property the formatter has to
guarantee whatever a caller passes - a test that only checks well-behaved
call sites would prove nothing about the careless one written next month.
"""

import json
import logging

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from documents.models import Document, ProcessingJob
from documents.observability import context, metrics, telemetry
from documents.observability.logging import (
    MAX_VALUE_LENGTH,
    REDACTED,
    StructuredFormatter,
    redact_value,
)
from documents.processing.pipeline import run_pipeline
from documents.tests import MEDIA_ROOT, make_user

SECRET = (
    "Clause 4.2 Attendance minimum is 75 percent for examination eligibility "
    "and the confidential board resolution number is XYZ-9981."
)


def format_record(**fields):
    """Format one log record exactly as the configured handler would."""
    record = logging.LogRecord(
        name="adi.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=fields.pop("message", "event"),
        args=(),
        exc_info=None,
    )
    for key, value in fields.items():
        setattr(record, key, value)
    return StructuredFormatter().format(record)


def counter_value(collector, **labels):
    """Read one labelled counter out of the private registry."""
    for metric in collector.collect():
        for sample in metric.samples:
            if not sample.name.endswith("_total"):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


# --- Redaction (§13) ---------------------------------------------------------


class RedactionTests(TestCase):
    def test_a_field_named_for_content_is_replaced_wholesale(self):
        """Truncating a question still leaks a question."""
        self.assertEqual(redact_value("question", SECRET), REDACTED)
        self.assertEqual(redact_value("answer", SECRET), REDACTED)
        self.assertEqual(redact_value("text", SECRET), REDACTED)

    def test_secrets_are_replaced(self):
        for key in ("password", "token", "api_key", "authorization"):
            self.assertEqual(redact_value(key, "hunter2"), REDACTED)

    def test_a_long_value_under_an_innocent_name_is_replaced_not_trimmed(self):
        """The careless call site: content passed under a harmless key.

        Truncation was the original behaviour and it leaked the first 120
        characters of the document - a leak with extra steps. The length is
        reported; none of the content is.
        """
        result = redact_value("detail", SECRET)

        self.assertNotIn("XYZ-9981", result)
        self.assertNotIn("Attendance", result)
        self.assertIn(str(len(SECRET)), result)

    def test_short_operational_values_survive_intact(self):
        """Over-redaction would make the logs useless."""
        self.assertEqual(redact_value("stage", "INGESTION"), "INGESTION")
        self.assertEqual(redact_value("error_code", "OCR_FAILED"), "OCR_FAILED")
        self.assertEqual(redact_value("document_id", 42), 42)
        self.assertEqual(redact_value("duration_ms", 12.5), 12.5)

    def test_nested_structures_are_redacted_too(self):
        result = redact_value("payload", {"question": SECRET, "stage": "OCR"})
        self.assertEqual(result["question"], REDACTED)
        self.assertEqual(result["stage"], "OCR")

    def test_lists_are_bounded_and_redacted(self):
        result = redact_value("items", [SECRET] * 50)
        self.assertLessEqual(len(result), 20)
        self.assertNotIn("XYZ-9981", " ".join(result))

    def test_the_document_text_never_reaches_the_log_line(self):
        """The end-to-end guarantee, tested the way a leak would happen."""
        line = format_record(
            message="rag.answered", question=SECRET, answer=SECRET, detail=SECRET
        )

        self.assertNotIn("XYZ-9981", line)
        self.assertNotIn("Attendance minimum", line)

    def test_interpolated_content_in_a_message_is_dropped(self):
        """`logger.info(f"answering {question}")` is the realistic leak.

        Our own loggers emit a single-token event name, so anything past
        that token is interpolated data and is removed rather than trimmed.
        """
        line = format_record(message=f"rag.answering {SECRET}")

        self.assertNotIn("XYZ-9981", line)
        self.assertNotIn("Attendance", line)
        self.assertIn("rag.answering", line)

    def test_a_plain_event_name_survives(self):
        payload = json.loads(format_record(message="pipeline.stage"))
        self.assertEqual(payload["message"], "pipeline.stage")

    def test_an_embedded_blob_in_a_message_is_replaced(self):
        line = format_record(message=f"processing {'A' * 500} done")
        self.assertNotIn("A" * 300, line)

    def test_output_is_valid_json(self):
        payload = json.loads(format_record(message="pipeline.stage", stage="OCR"))

        self.assertEqual(payload["message"], "pipeline.stage")
        self.assertEqual(payload["stage"], "OCR")
        self.assertEqual(payload["level"], "INFO")


# --- Correlation ids (§16, §20) ---------------------------------------------


class RequestIdTests(TestCase):
    def tearDown(self):
        context.reset_request_id()

    def test_an_id_is_minted_when_the_caller_sends_none(self):
        self.assertTrue(context.set_request_id(""))

    def test_a_caller_supplied_id_is_adopted(self):
        self.assertEqual(context.set_request_id("abc-123"), "abc-123")

    def test_a_hostile_id_cannot_inject_into_the_log_format(self):
        """The header lands in JSON lines other tools parse, so a caller
        must not be able to smuggle newlines or quotes into them."""
        cleaned = context.set_request_id('bad"\n{"level":"CRITICAL"}')

        self.assertNotIn('"', cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertTrue(cleaned)

    def test_an_over_long_id_is_capped(self):
        self.assertLessEqual(len(context.set_request_id("x" * 500)), 64)

    def test_the_id_appears_in_every_log_line(self):
        context.set_request_id("trace-me")
        payload = json.loads(format_record(message="anything"))
        self.assertEqual(payload["request_id"], "trace-me")


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class MiddlewareTests(TestCase):
    def setUp(self):
        self.user = make_user("obs_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_response_carries_the_request_id(self):
        response = self.client.get(reverse("document-list"))
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_a_supplied_request_id_is_echoed_back(self):
        response = self.client.get(
            reverse("document-list"), HTTP_X_REQUEST_ID="from-the-gateway"
        )
        self.assertEqual(response.headers["X-Request-ID"], "from-the-gateway")

    def test_requests_are_counted_by_route_not_by_path(self):
        """Using the resolved path would mint a new time series per
        document and eventually take the metrics backend down."""
        before = counter_value(
            metrics.http_requests, route="/api/documents/", status="200"
        )
        self.client.get(reverse("document-list"))
        after = counter_value(
            metrics.http_requests, route="/api/documents/", status="200"
        )

        self.assertEqual(after, before + 1)

    def test_a_detail_route_is_one_series_for_every_document(self):
        """Per-document labels are how instrumentation takes down the
        metrics backend it reports to."""
        from documents.observability.middleware import _route

        class Match:
            route = "api/documents/(?P<pk>[^/.]+)/"

        class Request:
            resolver_match = Match()

        self.assertEqual(_route(Request()), "/api/documents/{pk}/")

    def test_metrics_and_health_are_not_counted(self):
        """Scrape and probe traffic would bury the traffic anyone cares
        about."""
        before = counter_value(metrics.http_requests, route="/metrics")
        self.client.get("/metrics")
        self.assertEqual(counter_value(metrics.http_requests, route="/metrics"), before)


# --- The metrics endpoint ----------------------------------------------------


class MetricsEndpointTests(TestCase):
    def test_exposes_the_prometheus_text_format(self):
        response = self.client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response["Content-Type"])
        self.assertIn(b"adi_http_requests_total", response.content)

    def test_carries_no_document_content(self):
        """A counter can only hold a number; this pins that nothing has
        smuggled a label with free text into it."""
        body = self.client.get("/metrics").content.decode()
        self.assertNotIn("XYZ-9981", body)

    @override_settings(METRICS_ALLOWED_IPS=["10.0.0.1"])
    def test_can_be_restricted_by_address(self):
        self.assertEqual(self.client.get("/metrics").status_code, 403)

    @override_settings(METRICS_ALLOWED_IPS=["127.0.0.1"])
    def test_an_allowed_address_still_gets_through(self):
        self.assertEqual(self.client.get("/metrics").status_code, 200)


# --- Telemetry wired into the system ----------------------------------------


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PipelineTelemetryTests(TestCase):
    def setUp(self):
        self.user = make_user("telemetry_user", department="Registry")

    def make_job(self, payload=b"Attendance minimum is 75 percent.\n", name="d.txt"):
        document = Document.objects.create(
            title="Telemetry probe", owner=self.user, department="Registry"
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(payload, name=name),
            file_name=name,
            file_size=len(payload),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        return document, ProcessingJob.objects.create(document_version=version)

    def test_stage_outcomes_are_counted(self):
        before = counter_value(
            metrics.pipeline_stages, stage="INGESTION", status="SUCCEEDED"
        )
        _, job = self.make_job()
        run_pipeline(job.id)

        self.assertEqual(
            counter_value(metrics.pipeline_stages, stage="INGESTION", status="SUCCEEDED"),
            before + 1,
        )

    def test_job_outcome_is_counted(self):
        before = counter_value(metrics.pipeline_jobs, outcome="COMPLETED")
        _, job = self.make_job()
        run_pipeline(job.id)

        self.assertEqual(
            counter_value(metrics.pipeline_jobs, outcome="COMPLETED"), before + 1
        )

    def test_pages_are_counted_by_extraction_method(self):
        before = counter_value(metrics.pages_processed, extraction_method="PLAIN_TEXT")
        _, job = self.make_job()
        run_pipeline(job.id)

        self.assertEqual(
            counter_value(metrics.pages_processed, extraction_method="PLAIN_TEXT"),
            before + 1,
        )

    def test_a_failure_records_its_error_code_and_retryability(self):
        from documents.processing.errors import StageError

        before = counter_value(
            metrics.pipeline_failures,
            stage="VALIDATION",
            error_code="UNSUPPORTED_FILE_TYPE",
            retryable="False",
        )
        # An unsupported EXTENSION as well as unrecognised bytes: a .txt
        # name would legitimately fall back to plain text and succeed.
        _, job = self.make_job(b"\xff\xfe not a document", name="d.xyz")
        with self.assertRaises(StageError):
            run_pipeline(job.id)

        self.assertEqual(
            counter_value(
                metrics.pipeline_failures,
                stage="VALIDATION",
                error_code="UNSUPPORTED_FILE_TYPE",
                retryable="False",
            ),
            before + 1,
        )

    def test_telemetry_never_breaks_the_thing_it_measures(self):
        """Instrumentation must not be the reason a document fails."""
        telemetry.stage_finished(None, None, "not-a-number")
        telemetry.rag_answered(object())
        telemetry.job_finished(None)


class SecurityTelemetryTests(TestCase):
    def test_an_authorization_denial_is_counted(self):
        before = counter_value(metrics.authz_denials, action="DOCUMENT_VIEW")
        telemetry.authorization_denied("DOCUMENT_VIEW", user_id=1, document_id=2)

        self.assertEqual(
            counter_value(metrics.authz_denials, action="DOCUMENT_VIEW"), before + 1
        )

    def test_a_scan_verdict_is_counted(self):
        before = counter_value(metrics.malware_verdicts, verdict="INFECTED")
        telemetry.scan_verdict("INFECTED", document_id=1, findings=2)

        self.assertEqual(
            counter_value(metrics.malware_verdicts, verdict="INFECTED"), before + 1
        )
