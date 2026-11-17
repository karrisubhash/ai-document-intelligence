"""The new input types, end to end through the real pipeline.

Spreadsheets, delimited files, decks and multi-page TIFFs are uploaded through
the API and processed to completion, then read back from the database: the
pages with the right extraction method, the tables with their blank cells and
their source, and the chunks the search index is built from.
"""

import io
import shutil

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches
from rest_framework import status

from documents.models import (
    Document,
    DocumentChunk,
    DocumentFigure,
    DocumentTable,
    PageContent,
    ProcessingJob,
)
from documents.processing.errors import ErrorCode, StageError
from documents.tests import MEDIA_ROOT, AuthenticatedAPITestCase

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def xlsx_bytes(fill=None):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Fees"
    if fill is None:
        sheet.append(["Programme", "Tuition", "Exam Fee"])
        sheet.append(["B.Tech CSE", 85000, 2500])
        sheet.append(["B.Tech ECE", None, 2500])
        sheet.append(["M.Tech VLSI", 95000, 3000])
    else:
        fill(book)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def pptx_bytes(slides=2, notes="Remind them about the hall tickets."):
    prs = Presentation()
    for number in range(slides):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f"Exam schedule {number + 1}"
        if number == 0 and notes:
            slide.notes_slide.notes_text_frame.text = notes
        shape = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(5), Inches(1))
        shape.table.cell(0, 0).text = "Paper"
        shape.table.cell(0, 1).text = "Date"
        shape.table.cell(1, 0).text = "Maths"
        shape.table.cell(1, 1).text = "2024-05-03"
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def tiff_bytes(*texts):
    frames = []
    for text in texts:
        image = Image.new("RGB", (1400, 260), "white")
        ImageDraw.Draw(image).text(
            (40, 90), text, fill="black", font=ImageFont.load_default(size=60)
        )
        frames.append(image)
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


@override_settings(
    MEDIA_ROOT=MEDIA_ROOT,
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class NewFormatPipelineTests(AuthenticatedAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def _post(self, name, payload, mime):
        return self.client.post(
            reverse("document-list"),
            {
                "title": "Upload",
                "file": SimpleUploadedFile(name, payload, content_type=mime),
            },
            format="multipart",
        )

    def upload(self, name, payload, mime="application/octet-stream"):
        with self.captureOnCommitCallbacks(execute=True):
            response = self._post(name, payload, mime)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        return Document.objects.get(id=response.data["id"])

    def upload_expecting_failure(self, name, payload, mime="application/octet-stream"):
        with self.assertRaises(StageError):
            with self.captureOnCommitCallbacks(execute=True):
                response = self._post(name, payload, mime)
                self.document_id = response.data["id"]
        document = Document.objects.get(id=self.document_id)
        job = ProcessingJob.objects.get(document_version=document.current_version)
        return document, job

    def pages(self, document):
        return list(
            PageContent.objects.filter(
                content__document_version=document.current_version
            ).order_by("page_number")
        )

    # -- spreadsheets ----------------------------------------------------

    def test_a_workbook_is_stored_as_a_sheet_page_with_its_table(self):
        document = self.upload("fees.xlsx", xlsx_bytes(), XLSX)

        self.assertEqual(document.status, Document.Status.COMPLETED)
        (page,) = self.pages(document)
        self.assertEqual(
            page.extraction_method, PageContent.ExtractionMethod.SPREADSHEET
        )
        self.assertTrue(page.text.startswith("Sheet: Fees"))
        self.assertEqual(page.metadata["sheet"], "Fees")
        self.assertEqual(page.metadata["extractor"], "openpyxl")

        # The sheet's table is its labelled rows; the blank tuition cell is
        # left out of its row rather than shifting the exam fee into it.
        self.assertFalse(
            DocumentTable.objects.filter(
                document_version=document.current_version
            ).exists()
        )
        self.assertIn(
            "Table 1, row 2: Programme: B.Tech ECE; Exam Fee: 2500", page.text
        )

    def test_the_table_reaches_the_search_index_as_a_table_chunk(self):
        document = self.upload("fees.xlsx", xlsx_bytes(), XLSX)

        chunks = DocumentChunk.objects.filter(document_version=document.current_version)

        self.assertTrue(any(c.metadata.get("kind") == "TABLE" for c in chunks))

    def test_workbook_level_facts_land_on_the_content_row(self):
        document = self.upload("fees.xlsx", xlsx_bytes(), XLSX)

        content = document.current_version.contents.first()

        self.assertEqual(content.metadata["sheets"], 1)
        self.assertEqual(content.metadata["file_type"], "XLSX")
        self.assertIn("compression_ratio", content.metadata)

    def test_a_workbook_with_nothing_in_it_fails_honestly(self):
        document, job = self.upload_expecting_failure(
            "empty.xlsx", xlsx_bytes(fill=lambda book: None), XLSX
        )

        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertEqual(job.error_code, ErrorCode.EMPTY_EXTRACTION)

    def test_a_workbook_misnamed_as_a_docx_is_still_read_as_one(self):
        """The bytes decide, and the mismatch is recorded."""
        document = self.upload("report.docx", xlsx_bytes(), XLSX)

        self.assertEqual(document.status, Document.Status.COMPLETED)
        job = ProcessingJob.objects.get(document_version=document.current_version)
        event = job.events.get(stage=ProcessingJob.Stage.INGESTION)
        self.assertEqual(event.metadata["detected_type"], "XLSX")
        self.assertFalse(event.metadata["type_matches_extension"])

    # -- delimited -------------------------------------------------------

    def test_a_csv_is_read_as_a_table(self):
        payload = b"Course,Fee,Seats\nCS101,5000,60\nCS102,,45\n"

        document = self.upload("courses.csv", payload, "text/csv")

        (page,) = self.pages(document)
        self.assertEqual(
            page.extraction_method, PageContent.ExtractionMethod.SPREADSHEET
        )
        self.assertEqual(
            page.text.split(chr(10))[-2:],
            [
                "Table 1, row 1: Course: CS101; Fee: 5000; Seats: 60",
                "Table 1, row 2: Course: CS102; Seats: 45",
            ],
        )

    def test_a_tsv_is_read_as_a_table(self):
        document = self.upload(
            "courses.tsv",
            b"Course\tFee\nCS101\t5000\nCS102\t6000\n",
            "text/tab-separated-values",
        )

        (page,) = self.pages(document)
        self.assertIn("Table 1, row 1: Course: CS101; Fee: 5000", page.text)
        self.assertIn("Table 1, row 2: Course: CS102; Fee: 6000", page.text)

    def test_a_binary_file_renamed_csv_is_not_parsed_into_noise(self):
        document, job = self.upload_expecting_failure(
            "data.csv", b"\x89\x00\x01binary\x00junk" * 20, "text/csv"
        )

        self.assertEqual(job.error_code, ErrorCode.UNSUPPORTED_FILE_TYPE)

    # -- presentations ---------------------------------------------------

    def test_a_deck_is_a_page_per_slide_with_tables_and_notes(self):
        document = self.upload("exams.pptx", pptx_bytes(), PPTX)

        self.assertEqual(document.status, Document.Status.COMPLETED)
        first, second = self.pages(document)
        self.assertEqual(
            first.extraction_method, PageContent.ExtractionMethod.PRESENTATION
        )
        self.assertIn("Exam schedule 1", first.text)
        self.assertIn("Notes: Remind them about the hall tickets.", first.text)
        self.assertNotIn("Notes:", second.text)

        # One table per slide, numbered across the deck. A two-column table
        # with one row under its first is read as labels and values: no value
        # can be given another row's label that way.
        self.assertIn("Table 1, row 2: Maths: 2024-05-03", first.text)
        self.assertIn("Table 2, row 2: Maths: 2024-05-03", second.text)

    @override_settings(MAX_DOCUMENT_PAGES=2)
    def test_a_deck_over_the_page_limit_is_refused(self):
        document, job = self.upload_expecting_failure(
            "long.pptx", pptx_bytes(slides=3), PPTX
        )

        self.assertEqual(job.error_code, ErrorCode.TOO_MANY_PAGES)
        self.assertEqual(job.current_stage, ProcessingJob.Stage.VALIDATION)

    # -- multi-page images -----------------------------------------------

    def test_a_multi_page_tiff_is_read_page_by_page_with_a_figure_each(self):
        document = self.upload(
            "scan.tiff", tiff_bytes("FIRST PAGE", "SECOND PAGE"), "image/tiff"
        )

        first, second = self.pages(document)
        self.assertEqual(first.extraction_method, PageContent.ExtractionMethod.OCR)
        self.assertIn("FIRST PAGE", first.text)
        self.assertIn("SECOND PAGE", second.text)
        self.assertIn("confidence", first.metadata)

        figures = DocumentFigure.objects.filter(
            document_version=document.current_version
        ).order_by("page_number")
        self.assertEqual([f.page_number for f in figures], [1, 2])

    @override_settings(MAX_DOCUMENT_PAGES=1)
    def test_a_tiff_with_more_frames_than_the_page_limit_is_refused(self):
        document, job = self.upload_expecting_failure(
            "scan.tiff", tiff_bytes("ONE", "TWO"), "image/tiff"
        )

        self.assertEqual(job.error_code, ErrorCode.TOO_MANY_PAGES)
