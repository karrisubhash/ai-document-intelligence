import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from celery.exceptions import Retry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw, ImageFont
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import UserProfile
from documents.benchmarks import extraction
from documents.models import (
    AuditLog,
    Document,
    DocumentContent,
    PageContent,
    ProcessingEvent,
    ProcessingJob,
)
from documents.processing.context import FileType
from documents.processing.errors import ErrorCode, StageError, TransientError
from documents.processing.pipeline import PROCESSING_VERSION
from documents.processing.stages.ingestion import detect_file_type
from documents.tasks import is_retryable
from documents.tests import MEDIA_ROOT, make_user


# --- Fixture builders -------------------------------------------------------


def make_pdf_bytes(page_texts):
    """A minimal, valid multi-page PDF that carries a real text layer.

    Built by hand rather than with an authoring library so the tests stay
    dependency-free and the fixture is deterministic.
    """
    objects = []
    page_object_numbers = [4 + index * 2 for index in range(len(page_texts))]
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode()
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, text in enumerate(page_texts):
        content_number = 5 + index * 2
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_number} 0 R >>"
            ).encode()
        )
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 24 Tf 72 700 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()

    return bytes(out)


def render_text_image(text, size=(1400, 260)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=60)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    draw.text((40, 90), text, fill="black", font=font)
    return image


def make_png_bytes(text="INVOICE 12345"):
    buffer = BytesIO()
    render_text_image(text).save(buffer, format="PNG")
    return buffer.getvalue()


def make_scanned_pdf_bytes(text="SCANNED NOTICE"):
    """An image-only PDF - no text layer, so it must go through OCR."""
    buffer = BytesIO()
    render_text_image(text).save(buffer, format="PDF")
    return buffer.getvalue()


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class IngestionTestCase(APITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.user = make_user("cs_faculty", UserProfile.Role.FACULTY, "Computer Science")
        self.client.force_authenticate(user=self.user)

    def upload(self, filename, content, content_type, title="Ingestion Fixture"):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("document-list"),
                {
                    "title": title,
                    "file": SimpleUploadedFile(filename, content, content_type=content_type),
                },
                format="multipart",
            )

    def upload_expecting_failure(self, filename, content, content_type):
        # Permanent failures propagate as StageError - the task does not
        # retry a document that cannot ever succeed.
        with self.assertRaises(StageError):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("document-list"),
                    {
                        "title": "Broken Fixture",
                        "file": SimpleUploadedFile(
                            filename, content, content_type=content_type
                        ),
                    },
                    format="multipart",
                )
        return response

    def content_for(self, document_id):
        document = Document.objects.get(id=document_id)
        return document.current_version.contents.first()

    def job_for(self, document_id):
        return ProcessingJob.objects.filter(
            document_version__document_id=document_id
        ).first()


class PagePreservationTests(IngestionTestCase):
    def test_multi_page_pdf_preserves_each_page(self):
        response = self.upload(
            "regulations.pdf",
            make_pdf_bytes(["Attendance rules", "Grading policy", "Exam conduct"]),
            "application/pdf",
        )

        content = self.content_for(response.data["id"])
        pages = list(content.pages.all())

        self.assertEqual(content.page_count, 3)
        self.assertEqual([page.page_number for page in pages], [1, 2, 3])
        self.assertIn("Attendance", pages[0].text)
        self.assertIn("Grading", pages[1].text)
        self.assertIn("Exam", pages[2].text)

    def test_pdf_text_layer_records_pdf_text_method(self):
        response = self.upload(
            "born_digital.pdf", make_pdf_bytes(["Born digital text"]), "application/pdf"
        )

        page = self.content_for(response.data["id"]).pages.first()

        self.assertEqual(page.extraction_method, PageContent.ExtractionMethod.PDF_TEXT)
        self.assertEqual(page.metadata["extractor"], "pypdf")
        self.assertEqual(page.character_count, len(page.text))

    def test_plain_text_produces_one_page(self):
        response = self.upload(
            "policy.txt", b"Minimum attendance is 75 percent.", "text/plain"
        )

        content = self.content_for(response.data["id"])
        page = content.pages.first()

        self.assertEqual(content.page_count, 1)
        self.assertEqual(page.extraction_method, PageContent.ExtractionMethod.PLAIN_TEXT)
        self.assertIn("75 percent", page.text)

    def test_extracted_text_remains_available_on_the_version(self):
        response = self.upload(
            "regulations.pdf",
            make_pdf_bytes(["First page body", "Second page body"]),
            "application/pdf",
        )

        version = Document.objects.get(id=response.data["id"]).current_version

        self.assertIn("First page body", version.extracted_text)
        self.assertIn("Second page body", version.extracted_text)


class OcrPathTests(IngestionTestCase):
    def test_image_is_extracted_via_ocr(self):
        response = self.upload("receipt.png", make_png_bytes(), "image/png")

        content = self.content_for(response.data["id"])
        page = content.pages.first()

        self.assertEqual(content.page_count, 1)
        self.assertEqual(page.extraction_method, PageContent.ExtractionMethod.OCR)
        self.assertTrue(page.text.strip())
        self.assertEqual(page.metadata["extractor"], "tesseract")

    def test_scanned_pdf_falls_back_to_ocr_per_page(self):
        response = self.upload(
            "scanned.pdf", make_scanned_pdf_bytes(), "application/pdf"
        )

        page = self.content_for(response.data["id"]).pages.first()

        self.assertEqual(page.extraction_method, PageContent.ExtractionMethod.OCR)
        self.assertTrue(page.text.strip())
        self.assertEqual(page.metadata["dpi"], 200)

    def test_text_and_ocr_are_separately_measurable(self):
        response = self.upload(
            "born_digital.pdf", make_pdf_bytes(["Digital text"]), "application/pdf"
        )
        job = self.job_for(response.data["id"])

        stages = list(job.events.values_list("stage", flat=True))
        self.assertEqual(
            stages,
            [
                ProcessingJob.Stage.INGESTION,
                ProcessingJob.Stage.VALIDATION,
                ProcessingJob.Stage.TEXT_EXTRACTION,
                ProcessingJob.Stage.OCR,
                ProcessingJob.Stage.CLASSIFICATION,
            ],
        )

        text_event = job.events.get(stage=ProcessingJob.Stage.TEXT_EXTRACTION)
        ocr_event = job.events.get(stage=ProcessingJob.Stage.OCR)

        self.assertEqual(text_event.metadata["pages_with_text_layer"], 1)
        self.assertEqual(ocr_event.metadata["pages_ocred"], 0)
        self.assertIn("duration_seconds", text_event.metadata)
        self.assertIn("duration_seconds", ocr_event.metadata)


class FileValidationTests(IngestionTestCase):
    def test_unsupported_type_is_rejected_at_validation(self):
        response = self.upload_expecting_failure(
            "notes.xyz", b"not a supported document", "application/octet-stream"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(job.error_code, "UNSUPPORTED_FILE_TYPE")
        self.assertEqual(job.current_stage, ProcessingJob.Stage.VALIDATION)
        # Failed before any extraction work was attempted.
        self.assertFalse(
            job.events.filter(stage=ProcessingJob.Stage.TEXT_EXTRACTION).exists()
        )

    @override_settings(MAX_DOCUMENT_SIZE_BYTES=100)
    def test_oversized_file_is_rejected(self):
        response = self.upload_expecting_failure(
            "big.pdf", make_pdf_bytes(["Body text " * 200]), "application/pdf"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.error_code, "FILE_TOO_LARGE")

    @override_settings(MAX_DOCUMENT_PAGES=2)
    def test_page_limit_is_enforced(self):
        response = self.upload_expecting_failure(
            "long.pdf", make_pdf_bytes(["a", "b", "c"]), "application/pdf"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.error_code, "TOO_MANY_PAGES")

    def test_corrupt_pdf_is_rejected(self):
        response = self.upload_expecting_failure(
            "broken.pdf", b"%PDF-1.4\nthis is not really a pdf", "application/pdf"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.error_code, "CORRUPT_DOCUMENT")

    def test_blank_document_reports_empty_extraction(self):
        response = self.upload_expecting_failure("blank.txt", b"   ", "text/plain")
        job = self.job_for(response.data["id"])

        self.assertEqual(job.error_code, "EMPTY_EXTRACTION")
        self.assertEqual(job.current_stage, ProcessingJob.Stage.OCR)

    def test_document_survives_a_failed_pipeline(self):
        response = self.upload_expecting_failure(
            "notes.xyz", b"unsupported", "application/octet-stream"
        )
        document = Document.objects.get(id=response.data["id"])

        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertEqual(document.versions.count(), 1)


class FailureTaxonomyTests(IngestionTestCase):
    """Permanent failures must not burn worker capacity on retries."""

    PERMANENT_CODES = [
        ErrorCode.UNSUPPORTED_FILE_TYPE,
        ErrorCode.FILE_TOO_LARGE,
        ErrorCode.TOO_MANY_PAGES,
        ErrorCode.CORRUPT_DOCUMENT,
        ErrorCode.EMPTY_FILE,
        ErrorCode.MISSING_FILE,
        ErrorCode.EMPTY_EXTRACTION,
    ]

    def test_document_failures_are_classified_permanent(self):
        for code in self.PERMANENT_CODES:
            with self.subTest(code=code):
                self.assertFalse(is_retryable(StageError("nope", code=code)))

    def test_infrastructure_failures_are_classified_transient(self):
        self.assertTrue(is_retryable(TransientError("storage blipped")))
        self.assertTrue(is_retryable(OperationalError("connection lost")))

    def test_programming_errors_are_not_retried(self):
        # A bug in our own code fails identically every attempt; retrying
        # it just multiplies the damage.
        self.assertFalse(is_retryable(TypeError("bug")))
        self.assertFalse(is_retryable(ValueError("bug")))

    def test_permanent_failure_uses_exactly_one_attempt(self):
        response = self.upload_expecting_failure(
            "notes.xyz", b"unsupported payload", "application/octet-stream"
        )
        job = self.job_for(response.data["id"])

        self.assertEqual(job.status, ProcessingJob.Status.FAILED)
        self.assertEqual(job.error_code, ErrorCode.UNSUPPORTED_FILE_TYPE)
        self.assertEqual(job.attempt, 1)

    def test_permanent_failure_records_each_stage_once(self):
        response = self.upload_expecting_failure(
            "notes.xyz", b"unsupported payload", "application/octet-stream"
        )
        job = self.job_for(response.data["id"])

        # Previously this read INGESTION, VALIDATION, INGESTION, VALIDATION...
        # once per wasted retry. The history is now truthful.
        self.assertEqual(
            list(job.events.values_list("stage", flat=True)),
            [ProcessingJob.Stage.INGESTION, ProcessingJob.Stage.VALIDATION],
        )

    def test_transient_failure_is_retried(self):
        # Patched at a call-time lookup inside the stage; the STAGES list
        # captures stage functions by reference at import time.
        stage_path = "documents.processing.stages.ingestion.detect_file_type"
        with mock.patch(stage_path, side_effect=TransientError("storage blipped")):
            with self.assertRaises(Retry):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post(
                        reverse("document-list"),
                        {
                            "title": "Transient Fixture",
                            "file": SimpleUploadedFile(
                                "doc.pdf",
                                make_pdf_bytes(["text"]),
                                content_type="application/pdf",
                            ),
                        },
                        format="multipart",
                    )


class FileTypeDetectionTests(SimpleTestCase):
    """Type detection must not trust the client-supplied filename."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="detect-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def write(self, name, content):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_detects_pdf_by_magic_bytes(self):
        self.assertEqual(
            detect_file_type(self.write("a.pdf", make_pdf_bytes(["x"]))), FileType.PDF
        )

    def test_detects_png_by_magic_bytes(self):
        self.assertEqual(
            detect_file_type(self.write("a.png", make_png_bytes())), FileType.IMAGE
        )

    def test_detects_jpeg_by_magic_bytes(self):
        buffer = BytesIO()
        render_text_image("JPEG").save(buffer, format="JPEG")
        self.assertEqual(
            detect_file_type(self.write("a.jpg", buffer.getvalue())), FileType.IMAGE
        )

    def test_pdf_renamed_as_txt_is_still_detected_as_pdf(self):
        path = self.write("disguised.txt", make_pdf_bytes(["hidden"]))
        self.assertEqual(detect_file_type(path), FileType.PDF)

    def test_plain_text_falls_back_to_extension(self):
        self.assertEqual(
            detect_file_type(self.write("a.txt", b"just words")), FileType.TXT
        )

    def test_unknown_binary_is_not_typed(self):
        self.assertIsNone(detect_file_type(self.write("a.xyz", b"\x00\x01\x02binary")))


class ContentProvenanceTests(IngestionTestCase):
    def test_content_is_bound_to_the_exact_version(self):
        response = self.upload(
            "v1.pdf", make_pdf_bytes(["Version one text"]), "application/pdf"
        )
        document = Document.objects.get(id=response.data["id"])
        version_1 = document.current_version

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("document-versions", args=[document.id]),
                {
                    "file": SimpleUploadedFile(
                        "v2.pdf",
                        make_pdf_bytes(["Version two text"]),
                        content_type="application/pdf",
                    )
                },
                format="multipart",
            )

        document.refresh_from_db()
        version_2 = document.current_version

        self.assertNotEqual(version_1.id, version_2.id)
        self.assertIn("Version one", version_1.contents.first().pages.first().text)
        self.assertIn("Version two", version_2.contents.first().pages.first().text)

    def test_reprocess_adds_content_without_destroying_the_previous_run(self):
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Reprocess me"]), "application/pdf"
        )
        document = Document.objects.get(id=response.data["id"])

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("document-reprocess", args=[document.id]))

        contents = document.current_version.contents.all()
        self.assertEqual(contents.count(), 2)
        self.assertNotEqual(contents[0].processing_job_id, contents[1].processing_job_id)

    def test_processing_version_is_recorded(self):
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Provenance"]), "application/pdf"
        )
        content = self.content_for(response.data["id"])

        self.assertEqual(content.processing_version, PROCESSING_VERSION)
        self.assertEqual(content.metadata["file_type"], FileType.PDF)
        self.assertIsNotNone(content.processing_job_id)


class ContentEndpointTests(IngestionTestCase):
    def test_returns_page_level_content(self):
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Page one", "Page two"]), "application/pdf"
        )
        document_id = response.data["id"]

        result = self.client.get(reverse("document-content", args=[document_id]))

        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(result.data["page_count"], 2)
        self.assertEqual(result.data["processing_version"], PROCESSING_VERSION)
        self.assertEqual(
            [page["page_number"] for page in result.data["pages"]], [1, 2]
        )
        self.assertEqual(
            result.data["pages"][0]["extraction_method"],
            PageContent.ExtractionMethod.PDF_TEXT,
        )

    def test_requires_authentication(self):
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Secret"]), "application/pdf"
        )
        self.client.force_authenticate(user=None)

        result = self.client.get(
            reverse("document-content", args=[response.data["id"]])
        )

        self.assertEqual(result.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_denied_across_departments_and_audited(self):
        response = self.upload(
            "doc.pdf", make_pdf_bytes(["Confidential"]), "application/pdf"
        )
        document_id = response.data["id"]

        outsider = make_user(
            "mech_faculty", UserProfile.Role.FACULTY, "Mechanical Engineering"
        )
        self.client.force_authenticate(user=outsider)
        result = self.client.get(reverse("document-content", args=[document_id]))

        self.assertEqual(result.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            AuditLog.objects.filter(
                user=outsider,
                action=AuditLog.Action.DOCUMENT_VIEW,
                document_id=document_id,
                result=AuditLog.Result.DENIED,
            ).exists()
        )


class BenchmarkHarnessTests(SimpleTestCase):
    def setUp(self):
        self.dataset = Path(tempfile.mkdtemp(prefix="benchmark-"))
        self.addCleanup(shutil.rmtree, self.dataset, True)

        born_digital = self.dataset / "born_digital"
        born_digital.mkdir()
        (born_digital / "sample.pdf").write_bytes(
            make_pdf_bytes(["Attendance requirement is 75 percent"])
        )
        (born_digital / "sample.txt").write_text(
            "Attendance requirement is 75 percent", encoding="utf-8"
        )

    def test_discovers_documents_and_categories(self):
        found = extraction.discover_documents(self.dataset)

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "born_digital")

    def test_ground_truth_is_picked_up_from_sibling_txt(self):
        path = self.dataset / "born_digital" / "sample.pdf"
        self.assertIsNotNone(extraction.ground_truth_for(path))

    def test_scoring_rewards_an_exact_match(self):
        similarity, word_accuracy = extraction.score_against_truth(
            "Attendance requirement is 75 percent",
            "Attendance requirement is 75 percent",
        )
        self.assertEqual(similarity, 1.0)
        self.assertEqual(word_accuracy, 1.0)

    def test_scoring_penalises_a_bad_extraction(self):
        similarity, _ = extraction.score_against_truth(
            "completely different text", "Attendance requirement is 75 percent"
        )
        self.assertLess(similarity, 0.5)

    def test_run_measures_a_document_against_ground_truth(self):
        result = extraction.run_one(
            self.dataset / "born_digital" / "sample.pdf", "born_digital", "pdf_text"
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(result.page_count, 1)
        self.assertGreater(result.similarity, 0.9)
        self.assertGreater(result.duration_seconds, 0)

    def test_failures_are_recorded_with_an_error_code(self):
        broken = self.dataset / "born_digital" / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4\nnot actually a pdf")

        result = extraction.run_one(broken, "born_digital", "pdf_text")

        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_code, "CORRUPT_DOCUMENT")

    def test_summary_reports_per_approach_totals(self):
        results = extraction.run_benchmark(self.dataset, ["pdf_text"])
        summary = extraction.summarize(results)

        self.assertEqual(summary["pdf_text"]["documents"], 1)
        self.assertEqual(summary["pdf_text"]["succeeded"], 1)
        self.assertIsNotNone(summary["pdf_text"]["mean_similarity"])
