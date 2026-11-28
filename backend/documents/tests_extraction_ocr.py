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
import types
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
from documents.processing.errors import ErrorCode, StageError
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
        # Something to judge by - a handful of doubtful words, not a blank page.
        poor = (
            [geometry.Word("xqz", i * 20, i * 20 + 15, 0, 10) for i in range(4)],
            31.0,
        )
        good = (
            [
                geometry.Word(word, i * 50, i * 50 + 40, 0, 10)
                for i, word in enumerate("Registrar of the examination cell".split())
            ],
            93.0,
        )

        with (
            mock.patch.object(ocr, "_read", side_effect=[poor, good]),
            mock.patch.object(ocr, "_orientation", return_value=90),
            # The block reading that follows a poor one is not what this tests.
            mock.patch.object(ocr, "_recognise", return_value=([], None)),
        ):
            page = ocr.read_page(text_image("anything"))

        self.assertEqual(page.rotation, 90)
        self.assertIn("Registrar of the examination cell", page.text)

    def test_a_worse_rotated_reading_is_not_kept(self):
        """Orientation detection can be wrong; the first reading stands
        unless turning the page actually reads better."""
        first = (
            [geometry.Word("Registrar", i * 70, i * 70 + 60, 0, 10) for i in range(4)],
            55.0,
        )
        worse = (
            [geometry.Word("xq", i * 20, i * 20 + 10, 0, 10) for i in range(3)],
            20.0,
        )
        readings = iter([first])

        def read(image, languages):
            return next(readings, worse)

        with (
            mock.patch.object(ocr, "_read", side_effect=read),
            mock.patch.object(ocr, "_orientation", return_value=180),
            mock.patch.object(ocr, "_recognise", return_value=([], None)),
        ):
            page = ocr.read_page(text_image("anything"))

        self.assertEqual(page.rotation, 0)
        self.assertEqual(page.text, "Registrar Registrar Registrar Registrar")

    def test_when_the_guess_is_wrong_the_other_turns_are_tried(self):
        """Tesseract's orientation guess is often absent on dense small print;
        a page that still reads as nonsense is turned until it reads."""
        junk = (
            [
                geometry.Word(token, i * 20, i * 20 + 15, 0, 10, confidence=40.0)
                for i, token in enumerate("a b c d e f g h i j k l".split())
            ],
            60.0,
        )
        clean = (
            [
                geometry.Word(w, i * 60, i * 60 + 50, 0, 10, confidence=95.0)
                for i, w in enumerate(
                    "Precinct report for the county registrar office".split()
                )
            ],
            95.0,
        )
        tried = []

        def read(image, languages):
            tried.append(image.size)
            # The turned-270 page is landscape (3000 wide); only it reads.
            return clean if image.size == (3000, 2000) and len(tried) == 3 else junk

        with (
            mock.patch.object(ocr, "_read", side_effect=read),
            mock.patch.object(ocr, "_orientation", return_value=0),
            mock.patch.object(ocr, "_recognise", return_value=([], None)),
        ):
            page = ocr.read_page(Image.new("L", (2000, 3000), 255))

        # upright, then 90 (junk), then 270 (reads) - and it stops there.
        self.assertEqual(len(tried), 3)
        self.assertEqual(page.rotation, 270)
        self.assertIn("Precinct report", page.text)

    def test_a_well_read_page_is_never_turned(self):
        clean = (
            [
                geometry.Word(w, i * 60, i * 60 + 50, 0, 10, confidence=95.0)
                for i, w in enumerate(
                    "Precinct report for the county registrar office".split()
                )
            ],
            95.0,
        )

        with (
            mock.patch.object(ocr, "_read", return_value=clean) as read,
            mock.patch.object(ocr, "_orientation") as detect,
        ):
            ocr.read_page(Image.new("L", (2000, 3000), 255))

        read.assert_called_once()
        detect.assert_not_called()

    def test_a_numeric_table_read_confidently_is_not_suspected(self):
        numbers = (
            [
                geometry.Word(str(n % 90), i * 12, i * 12 + 8, 0, 10, confidence=93.0)
                for i, n in enumerate(range(60))
            ],
            93.0,
        )

        self.assertFalse(ocr._suspect(*numbers))

    def test_plausible_nonsense_is_suspected(self):
        nonsense = (
            [
                geometry.Word(token, i * 12, i * 12 + 8, 0, 10, confidence=55.0)
                for i, token in enumerate("| = a o 6 3 8 F i Q".split() * 4)
            ],
            63.0,
        )

        self.assertTrue(ocr._suspect(*nonsense))

    def test_unsure_orientation_changes_nothing(self):
        osd = {"rotate": 90, "orientation_conf": 0.1}

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


class FallbackTests(SimpleTestCase):
    """Tesseract's automatic layout sometimes finds no text at all in a page it
    reads perfectly well a third smaller or as one block. A reader that
    silently returns nothing is not acceptable."""

    GOOD = (
        [
            geometry.Word(w, 100 + i * 60, 160 + i * 60, 50, 60)
            for i, w in enumerate("One two three four five six".split())
        ],
        93.0,
    )
    NOTHING = ([], None)

    def image(self):
        return Image.new("L", (3000, 2000), 255)

    def test_a_good_first_reading_is_the_only_one(self):
        with mock.patch.object(ocr, "_recognise", return_value=self.GOOD) as read:
            ocr._read(self.image(), "eng")

        read.assert_called_once()

    def test_an_empty_reading_is_retried_smaller_and_scaled_back(self):
        with mock.patch.object(
            ocr, "_recognise", side_effect=[self.NOTHING, self.GOOD]
        ) as read:
            words, confidence = ocr._read(self.image(), "eng")

        self.assertEqual(read.call_count, 2)
        smaller = read.call_args_list[1].args[0]
        self.assertEqual(smaller.size, (2010, 1340))
        # Words found in the smaller image are placed back in the full-size one.
        self.assertAlmostEqual(words[0].x0, 100 / ocr.FALLBACK_SCALE)
        self.assertEqual(confidence, 93.0)

    def test_the_better_of_two_poor_readings_is_kept(self):
        weak = ([geometry.Word("xq", 0, 10, 0, 10)], 40.0)
        stronger = (
            [
                geometry.Word(w, i * 50, i * 50 + 40, 0, 10)
                for i, w in enumerate("a b c".split())
            ],
            55.0,
        )

        with mock.patch.object(ocr, "_recognise", side_effect=[weak, stronger, weak]):
            words, confidence = ocr._read(self.image(), "eng")

        self.assertEqual(len(words), 3)

    def test_junk_does_not_outscore_a_few_real_words(self):
        """A page read sideways yields plenty of tokens - at 30-45 percent, or
        as confident one-character bars and equals signs - and counting those
        lets junk outscore a clean reading with fewer words."""
        doubtful = [
            geometry.Word("xqz", i * 20, i * 20 + 15, 0, 10, confidence=38.0)
            for i in range(80)
        ]
        confident_noise = [
            geometry.Word("|", i * 20, i * 20 + 3, 0, 10, confidence=92.0)
            for i in range(80)
        ]
        clean = [
            geometry.Word(w, i * 60, i * 60 + 50, 0, 10, confidence=95.0)
            for i, w in enumerate("Registrar of the examination cell".split())
        ]

        self.assertGreater(ocr._worth(clean, 95.0), ocr._worth(doubtful, 38.0))
        self.assertGreater(ocr._worth(clean, 95.0), ocr._worth(confident_noise, 92.0))

    def test_reading_as_one_block_is_the_last_resort(self):
        order = []

        def recognise(image, languages, config=""):
            order.append(config or "auto")
            return self.GOOD if config else self.NOTHING

        with (
            mock.patch.object(ocr, "_recognise", side_effect=recognise),
            mock.patch.object(ocr, "_orientation") as detect,
        ):
            page = ocr.read_page(self.image())

        # The ordinary reading, that again smaller - then the block. A page with
        # nothing on it has no orientation to find, so none is looked for.
        self.assertEqual(order, ["auto", "auto", "--psm 6"])
        detect.assert_not_called()
        self.assertEqual(page.words, 6)

    def test_a_blank_page_is_not_turned_four_ways(self):
        with (
            mock.patch.object(ocr, "_recognise", return_value=self.NOTHING) as read,
            mock.patch.object(ocr, "_orientation") as detect,
        ):
            ocr.read_page(self.image())

        # Ordinary, smaller, block: three passes, however empty the page.
        self.assertEqual(read.call_count, 3)
        detect.assert_not_called()

    def test_when_every_attempt_finds_nothing_the_page_is_empty_not_an_error(self):
        with mock.patch.object(ocr, "_recognise", return_value=self.NOTHING):
            page = ocr.read_page(self.image())

        self.assertEqual(page.text, "")
        self.assertIsNone(page.confidence)

    def test_picture_words_are_scaled_back_to_the_picture_after_a_retry(self):
        picture = Image.new("L", (1500, 800), 255)

        with mock.patch.object(
            ocr, "_recognise", side_effect=[self.NOTHING, self.GOOD]
        ):
            words = ocr.read_words(picture)

        self.assertTrue(words)
        self.assertLess(max(w.x1 for w in words), 1500 + 1)


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


class OcrBudgetTests(SimpleTestCase):
    """A long scan must finish inside the task limit, so the stage trades
    resolution for time when the pages left will not fit. Time is a fake clock
    that each page advances by what it would cost at the resolution used."""

    COST = {300: 200.0, 250: 140.0, 200: 90.0, 150: 40.0, 100: 20.0}

    def run_stage(self, pages, budget, cost=None):
        cost = cost or self.COST
        clock = {"now": 0.0}
        used = []

        def page(path, number, dpi):
            used.append(dpi)
            clock["now"] += cost[dpi]
            return ocr.OcrPage(text=f"page {number}", words=3, confidence=90.0)

        context = IngestionContext(
            document_version=None,
            file_path="x.pdf",
            file_type=FileType.PDF,
            pages=[ExtractedPage(page_number=n) for n in range(1, pages + 1)],
        )
        with (
            override_settings(OCR_TIME_BUDGET_SECONDS=budget, OCR_DPI=300),
            mock.patch.object(ocr, "ocr_pdf_page", side_effect=page),
            # The stage's own reference to `time`, not the module itself: patching
            # time.monotonic would change the clock for everything running.
            mock.patch.object(
                ocr_stage,
                "time",
                types.SimpleNamespace(monotonic=lambda: clock["now"]),
            ),
        ):
            result = ocr_stage.run(context)
        return used, result, context

    def test_a_document_that_fits_is_read_at_full_resolution(self):
        used, result, _ = self.run_stage(pages=3, budget=2000)

        self.assertEqual(used, [300, 300, 300])
        self.assertNotIn("dpi_lowered_from", result.metadata)

    def test_a_long_scan_steps_down_until_it_fits(self):
        used, result, context = self.run_stage(pages=10, budget=1000)

        # Each step down is judged on what it actually cost: 300 and 250 and
        # 200 would each leave too little, 150 fits.
        self.assertEqual(used[:4], [300, 250, 200, 150])
        self.assertEqual(set(used[3:]), {150})
        self.assertEqual(result.metadata["dpi_lowered_from"], 300)
        self.assertEqual(result.metadata["dpi_final"], 150)
        self.assertIn("lowered from 300 to 150", result.message)
        # Each page records the resolution it was actually read at.
        self.assertEqual([p.metadata["dpi"] for p in context.pages], used)

    def test_it_never_goes_below_the_lowest_resolution(self):
        used, _, _ = self.run_stage(pages=6, budget=1)

        self.assertEqual(min(used), 100)
        self.assertEqual(used[-1], 100)

    def test_the_last_page_is_never_made_cheaper_for_nobody(self):
        used, result, _ = self.run_stage(pages=1, budget=1)

        self.assertEqual(used, [300])
        self.assertNotIn("dpi_lowered_from", result.metadata)


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

    def test_a_legacy_font_document_ocr_could_not_read_says_why(self):
        reason = "legacy Telugu font (prabhava): its text layer is not readable text"
        context = self.context()
        context.pages[0].metadata = {"ocr_reason": reason}

        with mock.patch.object(ocr, "ocr_pdf_page", return_value=ocr.OcrPage()):
            with self.assertRaises(StageError) as raised:
                ocr_stage.run(context)

        self.assertEqual(raised.exception.code, ErrorCode.EMPTY_EXTRACTION)
        self.assertIn("legacy Telugu font (prabhava)", str(raised.exception))

    def test_a_blank_scan_keeps_the_plain_message(self):
        with mock.patch.object(ocr, "ocr_pdf_page", return_value=ocr.OcrPage()):
            with self.assertRaises(StageError) as raised:
                ocr_stage.run(self.context())

        self.assertEqual(
            str(raised.exception), "No text could be extracted from the document."
        )
