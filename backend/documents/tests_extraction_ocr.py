"""OCR: layout, orientation, image handling, and the stage that uses them.

Most of these run real Tesseract over images drawn in the test, because the
claims are about what comes back from a real scan: that a blank cell in a
scanned table stays blank, that a sideways page is read, that a phone photo
stored with an EXIF rotation tag is read upright. The decisions in between
(when to ask for orientation, whether to keep the rotated reading) are
tested with mocks so each branch is exercised on purpose.
"""

import os
import tempfile
from io import BytesIO
from unittest import mock

from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageDraw, ImageFont

from documents.processing.context import (
    ExtractedPage,
    FileType,
    IngestionContext,
    PageTable,
)
from documents.processing.extractors import geometry, ocr
from documents.processing.stages import ingestion, text_extraction
from documents.processing.stages import ocr as ocr_stage

FONT = ImageFont.load_default(size=28)


def table_image():
    """A scanned-looking fee table with a blank cell in the third row."""
    image = Image.new("RGB", (1500, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 40), "FEE STRUCTURE 2024", fill="black", font=FONT)
    rows = [
        ("Programme", "Tuition", "Exam Fee"),
        ("B.Tech CSE", "85000", "2500"),
        ("B.Tech ECE", "", "2500"),
        ("M.Tech VLSI", "95000", "3000"),
    ]
    for index, (name, tuition, exam) in enumerate(rows):
        y = 130 + index * 50
        draw.text((60, y), name, fill="black", font=FONT)
        if tuition:
            width = draw.textlength(tuition, font=FONT)
            draw.text((620 - width, y), tuition, fill="black", font=FONT)
        if exam:
            width = draw.textlength(exam, font=FONT)
            draw.text((900 - width, y), exam, fill="black", font=FONT)
    draw.text(
        (60, 400),
        "Note: all fees are payable before the semester begins.",
        fill="black",
        font=FONT,
    )
    return image


def text_image(text, size=(1400, 260)):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text(
        (40, 90), text, fill="black", font=ImageFont.load_default(size=60)
    )
    return image


EXPECTED_ROWS = [
    ["Programme", "Tuition", "Exam Fee"],
    ["B.Tech CSE", "85000", "2500"],
    ["B.Tech ECE", "", "2500"],
    ["M.Tech VLSI", "95000", "3000"],
]


class ScannedLayoutTests(SimpleTestCase):
    def test_a_scanned_table_keeps_its_blank_cell(self):
        page = ocr.read_page(table_image())

        self.assertEqual(len(page.tables), 1)
        self.assertEqual(page.tables[0].rows, EXPECTED_ROWS)
        self.assertEqual(page.tables[0].source, "tesseract:layout")
        self.assertIn("B.Tech ECE |  | 2500", page.text)

    def test_the_title_and_note_stay_as_text_around_the_table(self):
        page = ocr.read_page(table_image())

        table = page.tables[0]
        self.assertEqual(
            page.text[: table.character_start].strip(), "FEE STRUCTURE 2024"
        )
        self.assertIn(
            "Note: all fees are payable before the semester begins.",
            page.text[table.character_end :],
        )

    def test_confidence_and_word_count_are_reported(self):
        page = ocr.read_page(table_image())

        self.assertGreater(page.confidence, 80)
        self.assertGreater(page.words, 20)
        self.assertEqual(page.rotation, 0)
        self.assertEqual(page.metadata["languages"], "eng")
        self.assertEqual(page.metadata["tables"], 1)

    def test_a_clean_upright_page_never_pays_for_orientation_detection(self):
        with mock.patch.object(ocr, "_orientation") as detect:
            ocr.read_page(table_image())

        detect.assert_not_called()


class OrientationTests(SimpleTestCase):
    def test_a_sideways_or_upside_down_page_is_turned_and_read(self):
        for angle in (90, 180, 270):
            with self.subTest(angle=angle):
                turned = table_image().rotate(angle, expand=True, fillcolor="white")

                page = ocr.read_page(turned)

                # Tesseract's own page segmentation reads some orientations
                # (270 here) correctly on the first pass, in which case no
                # turn is needed or recorded. What matters is the reading.
                self.assertIn(page.rotation, (0, angle))
                self.assertIn("FEE STRUCTURE 2024", page.text)
                self.assertEqual(page.tables[0].rows, EXPECTED_ROWS)

    def test_a_phone_photo_stored_sideways_is_read_upright(self):
        """The pixels are sideways; an EXIF tag says how to turn them."""
        sideways = table_image().rotate(90, expand=True, fillcolor="white")
        exif = Image.Exif()
        exif[0x0112] = 6  # display by rotating 90 degrees clockwise
        buffer = BytesIO()
        sideways.save(buffer, format="JPEG", exif=exif.tobytes())

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "photo.jpg")
            with open(path, "wb") as handle:
                handle.write(buffer.getvalue())

            with mock.patch.object(ocr, "_orientation") as detect:
                page = ocr.ocr_image_file(path)

        self.assertIn("FEE STRUCTURE 2024", page.text)
        detect.assert_not_called()  # the tag was enough; no second opinion needed

    def test_a_better_rotated_reading_replaces_the_first(self):
        poor = ([geometry.Word("xq", 0, 10, 0, 10)], 31.0)
        good = (
            [
                geometry.Word(word, i * 50, i * 50 + 40, 0, 10)
                for i, word in enumerate("Registrar of the examination cell".split())
            ],
            93.0,
        )

        with (
            mock.patch.object(ocr, "_recognise", side_effect=[poor, good]),
            mock.patch.object(ocr, "_orientation", return_value=90),
        ):
            page = ocr.read_page(text_image("anything"))

        self.assertEqual(page.rotation, 90)
        self.assertIn("Registrar of the examination cell", page.text)

    def test_a_worse_rotated_reading_is_not_kept(self):
        """Orientation detection can be wrong; the first reading stands
        unless turning the page actually reads better."""
        first = ([geometry.Word("Registrar", 0, 60, 0, 10)], 55.0)
        worse = ([geometry.Word("xq", 0, 10, 0, 10)], 20.0)

        with (
            mock.patch.object(ocr, "_recognise", side_effect=[first, worse]),
            mock.patch.object(ocr, "_orientation", return_value=180),
        ):
            page = ocr.read_page(text_image("anything"))

        self.assertEqual(page.rotation, 0)
        self.assertEqual(page.text, "Registrar")

    def test_unsure_orientation_changes_nothing(self):
        osd = {"rotate": 90, "orientation_conf": 0.4}

        with mock.patch.object(ocr.pytesseract, "image_to_osd", return_value=osd):
            self.assertEqual(ocr._orientation(text_image("x")), 0)

    def test_detection_that_errors_is_not_fatal(self):
        with mock.patch.object(
            ocr.pytesseract,
            "image_to_osd",
            side_effect=RuntimeError("Too few characters"),
        ):
            self.assertEqual(ocr._orientation(text_image("x")), 0)

    @override_settings(OCR_DETECT_ROTATION=False)
    def test_orientation_detection_can_be_switched_off(self):
        poor = ([geometry.Word("xq", 0, 10, 0, 10)], 20.0)

        with (
            mock.patch.object(ocr, "_recognise", return_value=poor),
            mock.patch.object(ocr, "_orientation") as detect,
        ):
            ocr.read_page(text_image("anything"))

        detect.assert_not_called()


class ImagePreparationTests(SimpleTestCase):
    def test_a_transparent_png_is_composited_on_white(self):
        """Left alone, transparent pixels read as black and swallow black text."""
        image = Image.new("RGBA", (1400, 260), (0, 0, 0, 0))
        ImageDraw.Draw(image).text(
            (40, 90),
            "INVOICE 12345",
            fill=(0, 0, 0, 255),
            font=ImageFont.load_default(size=60),
        )

        page = ocr.read_page(image)

        self.assertIn("INVOICE 12345", page.text)

    def test_a_small_image_is_enlarged(self):
        prepared, scale = ocr.prepare(Image.new("RGB", (600, 200), "white"))

        self.assertEqual(scale, 2.0)
        self.assertEqual(prepared.size, (1200, 400))
        self.assertEqual(prepared.mode, "L")

    def test_a_huge_image_is_reduced_to_a_sane_size(self):
        prepared, scale = ocr.prepare(Image.new("L", (9000, 6000), 255))

        self.assertLess(scale, 1)
        self.assertEqual(max(prepared.size), ocr.MAX_SIDE)

    def test_a_normal_page_is_left_at_its_size(self):
        prepared, scale = ocr.prepare(Image.new("L", (2480, 3508), 255))

        self.assertEqual(scale, 1.0)
        self.assertEqual(prepared.size, (2480, 3508))

    def test_words_come_back_in_the_pictures_own_coordinates(self):
        """A small picture is enlarged to be read; the caller maps words onto
        the page by the size it passed in, so the enlargement is undone."""
        image = Image.new("RGB", (800, 300), "white")
        ImageDraw.Draw(image).text(
            (100, 100),
            "SCANNED TABLE",
            fill="black",
            font=ImageFont.load_default(size=40),
        )

        words = ocr.read_words(image)

        self.assertTrue(words)
        for word in words:
            self.assertLessEqual(word.x1, 802)
            self.assertLessEqual(word.bottom, 302)
        self.assertAlmostEqual(words[0].x0, 100, delta=20)


class RecognitionTests(SimpleTestCase):
    def test_low_confidence_words_are_dropped(self):
        data = {
            "text": ["", "Registrar", "~#;", "Office"],
            "conf": ["-1", "96", "12", "91"],
            "left": [0, 10, 100, 150],
            "top": [0, 10, 10, 10],
            "width": [0, 80, 20, 60],
            "height": [0, 20, 20, 20],
        }

        with mock.patch.object(ocr.pytesseract, "image_to_data", return_value=data):
            words, confidence = ocr._recognise(Image.new("L", (10, 10)), "eng")

        self.assertEqual([w.text for w in words], ["Registrar", "Office"])
        self.assertEqual(confidence, 93.5)

    def test_nothing_read_is_reported_as_no_confidence(self):
        data = {
            "text": [""],
            "conf": ["-1"],
            "left": [0],
            "top": [0],
            "width": [0],
            "height": [0],
        }

        with mock.patch.object(ocr.pytesseract, "image_to_data", return_value=data):
            words, confidence = ocr._recognise(Image.new("L", (10, 10)), "eng")

        self.assertEqual((words, confidence), ([], None))

    def test_a_language_pack_that_is_not_installed_is_dropped(self):
        self.assertEqual(ocr.resolve_languages("eng+hin+zzz"), "eng")

    def test_with_no_installed_pack_requested_english_is_the_floor(self):
        self.assertEqual(ocr.resolve_languages("hin"), "eng")


class FileTests(SimpleTestCase):
    def two_frame_tiff(self, folder):
        path = os.path.join(folder, "scan.tiff")
        first = text_image("FIRST PAGE")
        second = text_image("SECOND PAGE")
        first.save(path, save_all=True, append_images=[second])
        return path

    def test_a_multi_page_tiff_has_a_page_per_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.two_frame_tiff(folder)

            self.assertEqual(ocr.image_frame_count(path), 2)
            self.assertIn("FIRST PAGE", ocr.ocr_image_file(path, frame=0).text)
            self.assertIn("SECOND PAGE", ocr.ocr_image_file(path, frame=1).text)

    def test_the_extraction_stage_defers_every_frame_to_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            context = IngestionContext(
                document_version=None,
                file_path=self.two_frame_tiff(folder),
                file_type=FileType.IMAGE,
            )

            result = text_extraction.run(context)

        self.assertEqual([p.page_number for p in context.pages], [1, 2])
        self.assertEqual(len(context.pages_needing_ocr()), 2)
        self.assertEqual(result.metadata["pages_needing_ocr"], 2)

    def test_tiff_webp_and_bmp_are_recognised_by_their_bytes(self):
        picture = text_image("HELLO")
        with tempfile.TemporaryDirectory() as folder:
            for extension, kind in (
                (".tif", "TIFF"),
                (".webp", "WEBP"),
                (".bmp", "BMP"),
            ):
                with self.subTest(kind=kind):
                    path = os.path.join(folder, "file" + extension)
                    picture.save(path, format=kind)
                    self.assertEqual(ingestion.detect_file_type(path), FileType.IMAGE)

    def test_a_text_file_that_starts_with_bm_is_not_a_bitmap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "notes.txt")
            with open(path, "w") as handle:
                handle.write("BMW service schedule for the college fleet\n")

            self.assertEqual(ingestion.detect_file_type(path), FileType.TXT)


class OcrStageTests(SimpleTestCase):
    def context(self):
        page = ExtractedPage(page_number=1, metadata={"ocr_reason": "no text layer"})
        return IngestionContext(
            document_version=None,
            file_path="x.pdf",
            file_type=FileType.PDF,
            pages=[page],
        )

    def test_the_stage_stores_text_tables_and_how_it_was_read(self):
        table = PageTable(
            character_start=0,
            character_end=11,
            rows=[["a", "1"], ["b", "2"]],
            source="tesseract:layout",
        )
        read = ocr.OcrPage(
            text="a | 1\nb | 2",
            tables=[table],
            confidence=91.26,
            rotation=90,
            words=4,
        )
        context = self.context()

        with mock.patch.object(ocr, "ocr_pdf_page", return_value=read):
            ocr_stage.run(context)

        page = context.pages[0]
        self.assertEqual(page.text, "a | 1\nb | 2")
        self.assertEqual(page.tables, [table])
        self.assertEqual(page.extraction_method, "OCR")
        self.assertEqual(page.metadata["confidence"], 91.3)
        self.assertEqual(page.metadata["rotation"], 90)
        self.assertEqual(page.metadata["extractor"], "tesseract")
        self.assertEqual(page.metadata["ocr_reason"], "no text layer")
