"""Old-format Office files: converted by LibreOffice, then read as the modern form.

Most of these tests replace the converter with a stand-in, so they run on a
machine without LibreOffice and pin everything AROUND the conversion: how a
legacy file is recognised, what happens when no converter is installed, when it
fails or hangs, that its working files are removed, and that the converted
file goes through the very extractor a modern file does. The last class
converts for real, and is skipped where LibreOffice is not installed.
"""

import io
import os
import shutil
import subprocess
import tempfile
from unittest import mock, skipUnless

from django.test import SimpleTestCase, override_settings
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from documents.processing.context import FileType, IngestionContext
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import legacy
from documents.processing.stages import ingestion, text_extraction

# What an OLE2 compound file starts with: .doc, .xls, .ppt and .msg all do.
OLE = legacy.OLE_SIGNATURE + b"\x00" * 600


def write(data: bytes, suffix: str) -> str:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    return handle.name


def docx_bytes(*paragraphs) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(rows) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Fees"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def pptx_bytes(title: str) -> bytes:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = title
    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


class FileCase(SimpleTestCase):
    def setUp(self):
        self.paths: list = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in self.paths:
            if os.path.exists(path):
                os.unlink(path)

    def file(self, data: bytes, suffix: str) -> str:
        path = write(data, suffix)
        self.paths.append(path)
        return path


class DetectionTests(FileCase):
    def detect(self, data: bytes, suffix: str):
        return ingestion.detect_file_type(self.file(data, suffix))

    def test_an_ole_container_is_the_type_its_extension_says(self):
        self.assertEqual(self.detect(OLE, ".doc"), FileType.DOC)
        self.assertEqual(self.detect(OLE, ".XLS"), FileType.XLS)
        self.assertEqual(self.detect(OLE, ".ppt"), FileType.PPT)

    def test_an_ole_container_with_no_known_extension_is_not_guessed(self):
        self.assertIsNone(self.detect(OLE, ".msg"))
        self.assertIsNone(self.detect(OLE, ".bin"))

    def test_a_doc_that_is_really_rich_text_is_rich_text(self):
        self.assertEqual(self.detect(b"{\\rtf1\\ansi Hello}", ".doc"), FileType.RTF)

    def test_a_doc_that_is_plain_text_is_text(self):
        self.assertEqual(self.detect(b"Just some words", ".doc"), FileType.TXT)

    def test_excels_web_page_saved_as_xls_is_html(self):
        page = b"<html><body><table><tr><td>a</td></tr></table></body></html>"
        self.assertEqual(self.detect(page, ".xls"), FileType.HTML)
        self.assertEqual(
            self.detect(b"<table><tr><td>1</td></tr></table>", ".xls"), FileType.HTML
        )

    def test_a_tab_separated_export_saved_as_xls_is_a_delimited_file(self):
        self.assertEqual(self.detect(b"Name\tMarks\nAsha\t92\n", ".xls"), FileType.CSV)

    def test_a_binary_that_is_not_an_ole_container_is_not_believed(self):
        self.assertIsNone(self.detect(bytes(range(256)) * 3, ".xls"))
        self.assertIsNone(self.detect(bytes(range(256)) * 3, ".doc"))

    def test_a_modern_file_with_an_old_name_is_the_modern_type(self):
        self.assertEqual(self.detect(docx_bytes("hi"), ".doc"), FileType.DOCX)
        self.assertEqual(self.detect(xlsx_bytes([["a", "b"]]), ".xls"), FileType.XLSX)
        self.assertEqual(self.detect(pptx_bytes("t"), ".ppt"), FileType.PPTX)

    def test_the_old_extensions_are_accepted_for_upload(self):
        from documents.uploads import SUPPORTED_EXTENSIONS

        for extension in (".doc", ".xls", ".ppt"):
            self.assertIn(extension, SUPPORTED_EXTENSIONS)


class ConverterStandIn:
    """Plays LibreOffice: writes `output` as the converted file."""

    def __init__(self, output: bytes | None, returncode: int = 0, stderr: bytes = b""):
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list = []
        self.work_dirs: list = []

    def __call__(self, command, **kwargs):
        self.commands.append((command, kwargs))
        outdir = command[command.index("--outdir") + 1]
        source = command[-1]
        self.work_dirs.append(os.path.dirname(source))
        if self.output is not None:
            target = command[command.index("--convert-to") + 1]
            with open(os.path.join(outdir, f"input.{target}"), "wb") as handle:
                handle.write(self.output)
        return subprocess.CompletedProcess(
            command, self.returncode, stdout=b"", stderr=self.stderr
        )


@override_settings(LIBREOFFICE_PATH="")
class ConversionTests(FileCase):
    def run_with(
        self, stand_in, kind=FileType.DOC, suffix=".doc", binary="/usr/bin/soffice"
    ):
        path = self.file(OLE, suffix)
        with (
            mock.patch.object(legacy.shutil, "which", return_value=binary),
            mock.patch.object(legacy.subprocess, "run", side_effect=stand_in),
        ):
            with legacy.converted(path, kind) as produced:
                with open(produced, "rb") as handle:
                    return handle.read(), stand_in

    def test_the_command_is_headless_with_its_own_profile_and_the_right_target(self):
        stand_in = ConverterStandIn(b"converted")
        data, stand_in = self.run_with(stand_in)
        self.assertEqual(data, b"converted")
        command, kwargs = stand_in.commands[0]
        self.assertIn("--headless", command)
        self.assertEqual(command[command.index("--convert-to") + 1], "docx")
        self.assertTrue(
            any(part.startswith("-env:UserInstallation=file://") for part in command)
        )
        self.assertTrue(command[-1].endswith("input.doc"))
        self.assertGreater(kwargs["timeout"], 0)
        # The converter is not handed the worker's environment.
        self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "LANG"})

    def test_each_kind_converts_to_its_modern_form(self):
        for kind, suffix, target in (
            (FileType.DOC, ".doc", "docx"),
            (FileType.XLS, ".xls", "xlsx"),
            (FileType.PPT, ".ppt", "pptx"),
        ):
            stand_in = ConverterStandIn(b"x")
            _, stand_in = self.run_with(stand_in, kind, suffix)
            command, _ = stand_in.commands[0]
            self.assertEqual(command[command.index("--convert-to") + 1], target)
            self.assertTrue(command[-1].endswith("input" + suffix))

    def test_the_working_files_are_removed_afterwards(self):
        stand_in = ConverterStandIn(b"x")
        _, stand_in = self.run_with(stand_in)
        self.assertFalse(os.path.exists(stand_in.work_dirs[0]))

    def test_the_working_files_are_removed_when_the_conversion_fails(self):
        stand_in = ConverterStandIn(
            None, returncode=1, stderr=b"Error: source file could not be loaded"
        )
        with self.assertRaises(StageError):
            self.run_with(stand_in)
        self.assertFalse(os.path.exists(stand_in.work_dirs[0]))

    def test_a_failed_conversion_is_a_corrupt_document_with_the_reason(self):
        stand_in = ConverterStandIn(
            None, returncode=1, stderr=b"Error: source file could not be loaded"
        )
        with self.assertRaises(StageError) as raised:
            self.run_with(stand_in)
        self.assertEqual(raised.exception.code, ErrorCode.CORRUPT_DOCUMENT)
        self.assertIn("could not be loaded", str(raised.exception))

    def test_success_without_an_output_file_is_a_failure_not_an_empty_document(self):
        with self.assertRaises(StageError) as raised:
            self.run_with(ConverterStandIn(None, returncode=0))
        self.assertEqual(raised.exception.code, ErrorCode.CORRUPT_DOCUMENT)

    def test_a_conversion_that_hangs_is_stopped_and_says_so(self):
        def hang(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaises(StageError) as raised:
            self.run_with(hang)
        self.assertEqual(raised.exception.code, ErrorCode.EXTRACTION_FAILED)
        self.assertIn("seconds", str(raised.exception))

    def test_a_converter_that_cannot_start_is_a_failed_extraction(self):
        def broken(command, **kwargs):
            raise OSError("exec format error")

        with self.assertRaises(StageError) as raised:
            self.run_with(broken)
        self.assertEqual(raised.exception.code, ErrorCode.EXTRACTION_FAILED)

    @override_settings(LEGACY_CONVERT_TIMEOUT=7)
    def test_the_time_limit_is_a_setting(self):
        _, stand_in = self.run_with(ConverterStandIn(b"x"))
        self.assertEqual(stand_in.commands[0][1]["timeout"], 7)

    def test_without_a_converter_the_message_says_what_to_do(self):
        path = self.file(OLE, ".xls")
        with mock.patch.object(legacy.shutil, "which", return_value=None):
            self.assertFalse(legacy.available())
            with self.assertRaises(StageError) as raised:
                with legacy.converted(path, FileType.XLS):
                    pass
        self.assertEqual(raised.exception.code, ErrorCode.UNSUPPORTED_FILE_TYPE)
        self.assertIn("Save it as .xlsx", str(raised.exception))

    @override_settings(LIBREOFFICE_PATH="/nowhere/soffice")
    def test_a_configured_path_that_does_not_exist_is_no_converter(self):
        self.assertFalse(legacy.available())


@override_settings(LIBREOFFICE_PATH="")
class ThroughTheStageTests(FileCase):
    """The converted file goes through the extractor a modern file does."""

    def extract(self, kind, suffix, converted: bytes):
        path = self.file(OLE, suffix)
        context = IngestionContext(
            document_version=mock.Mock(file_name=f"old{suffix}"),
            file_path=path,
            file_type=kind,
        )
        with (
            mock.patch.object(legacy.shutil, "which", return_value="/usr/bin/soffice"),
            mock.patch.object(
                legacy.subprocess, "run", side_effect=ConverterStandIn(converted)
            ),
        ):
            result = text_extraction.run(context)
        return context, result

    def test_a_doc_is_read_as_a_docx(self):
        context, result = self.extract(
            FileType.DOC, ".doc", docx_bytes("Exam rules", "Bring your hall ticket.")
        )
        self.assertIn("Bring your hall ticket.", context.pages[0].text)
        self.assertEqual(context.metadata["converted_from"], "DOC")
        self.assertEqual(result.metadata["converted_from"], "DOC")
        self.assertTrue(result.message.startswith("Converted from an old-format file."))
        self.assertEqual(context.pages[0].extraction_method, "DOCX_TEXT")

    def test_an_xls_is_read_as_an_xlsx_with_its_tables(self):
        context, _ = self.extract(
            FileType.XLS,
            ".xls",
            xlsx_bytes([["Course", "Fee"], ["CSE", 85000], ["ECE", 80000]]),
        )
        page = context.pages[0]
        self.assertEqual(page.extraction_method, "SPREADSHEET")
        self.assertEqual(page.tables[0].rows[1], ["CSE", "85000"])

    def test_a_ppt_is_read_as_a_pptx(self):
        context, _ = self.extract(FileType.PPT, ".ppt", pptx_bytes("Orientation day"))
        self.assertIn("Orientation day", context.pages[0].text)
        self.assertEqual(context.pages[0].extraction_method, "PRESENTATION")

    def test_the_stored_path_is_restored_even_when_reading_fails(self):
        path = self.file(OLE, ".doc")
        context = IngestionContext(
            document_version=mock.Mock(file_name="old.doc"),
            file_path=path,
            file_type=FileType.DOC,
        )
        with (
            mock.patch.object(legacy.shutil, "which", return_value="/usr/bin/soffice"),
            mock.patch.object(
                legacy.subprocess,
                "run",
                side_effect=ConverterStandIn(b"not a docx at all"),
            ),
            self.assertRaises(StageError),
        ):
            text_extraction.run(context)
        self.assertEqual(context.file_path, path)


@skipUnless(
    shutil.which("soffice") or shutil.which("libreoffice"),
    "LibreOffice is not installed",
)
class RealConversionTests(FileCase):
    """Converts for real: a modern file down to the old format by LibreOffice,
    then back through the pipeline's own path."""

    def to_legacy(self, data: bytes, suffix: str, target: str) -> str:
        source = self.file(data, suffix)
        outdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outdir, True)
        binary = shutil.which("soffice") or shutil.which("libreoffice")
        subprocess.run(
            [
                binary,
                f"-env:UserInstallation=file://{outdir}/profile",
                "--headless",
                "--convert-to",
                target,
                "--outdir",
                outdir,
                source,
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        extension = target.split(":")[0]
        produced = os.path.join(
            outdir, os.path.splitext(os.path.basename(source))[0] + "." + extension
        )
        with open(produced, "rb") as handle:
            data = handle.read()
        return self.file(data, "." + extension)

    def run_stage(self, path: str, kind: str):
        context = IngestionContext(
            document_version=mock.Mock(file_name=os.path.basename(path)),
            file_path=path,
            file_type=kind,
        )
        text_extraction.run(context)
        return context

    def test_a_real_doc_with_a_table(self):
        # Built from HTML: LibreOffice drops the table python-docx writes
        # (which has no table properties) on its way to .doc, so a fixture made
        # that way would test LibreOffice's export and not our reading.
        page = (
            b"<html><body><p>Exam rules</p><table border=1>"
            b"<tr><td>Course</td><td>Fee</td></tr>"
            b"<tr><td>CSE</td><td>85000</td></tr>"
            b"<tr><td>ECE</td><td>80000</td></tr></table></body></html>"
        )
        path = self.to_legacy(page, ".html", "doc:MS Word 97")
        self.assertEqual(ingestion.detect_file_type(path), FileType.DOC)
        context = self.run_stage(path, FileType.DOC)
        self.assertIn("Exam rules", context.pages[0].text)
        self.assertEqual(context.pages[0].tables[0].rows[1], ["CSE", "85000"])

    def test_a_real_xls_with_two_sheets(self):
        book = Workbook()
        book.active.title = "Fees"
        book.active.append(["Course", "Fee"])
        book.active.append(["CSE", 85000])
        rooms = book.create_sheet("Rooms")
        rooms.append(["Room", "Seats"])
        rooms.append(["A1", 60])
        buffer = io.BytesIO()
        book.save(buffer)
        path = self.to_legacy(buffer.getvalue(), ".xlsx", "xls")
        self.assertEqual(ingestion.detect_file_type(path), FileType.XLS)
        context = self.run_stage(path, FileType.XLS)
        self.assertEqual(len(context.pages), 2)
        self.assertTrue(context.pages[1].text.startswith("Sheet: Rooms"))

    def test_a_real_ppt(self):
        path = self.to_legacy(pptx_bytes("Orientation day"), ".pptx", "ppt")
        self.assertEqual(ingestion.detect_file_type(path), FileType.PPT)
        context = self.run_stage(path, FileType.PPT)
        self.assertIn("Orientation day", context.pages[0].text)
