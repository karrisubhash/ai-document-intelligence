"""PDF extraction: tables, columns, garbled pages, pictures, fallbacks.

Every fixture is a PDF built in the test (`pdf_fixtures`), so each case
states a layout in coordinates and the expected reading in words. The
expectations are the ones the old pypdf extractor got wrong - a blank cell
collapsing so a number lands in the wrong column, two columns interleaved
line by line - which is the reason these tests exist.
"""

import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase
from PIL import Image

from documents import pdf_fixtures as fx
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import geometry, pdf
from documents.processing.tables import detector


def run(*pages, **options):
    """Extract a PDF built from `pages`; returns the list of PdfPage."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path, **options)


def slice_of(page, table):
    return page.text[table.character_start : table.character_end]


def fee_page():
    page = fx.Page().text(72, 60, "FEE STRUCTURE 2024", size=14, font="F2")
    page.grid(
        72,
        100,
        [150, 100, 100],
        [
            ["Programme", "Tuition", "Exam Fee"],
            ["B.Tech CSE", "85000", "2500"],
            ["B.Tech ECE", "", "2500"],
            ["Hostel (merged)", "", ""],
        ],
        right_align={1, 2},
        merge_right={(3, 1)},
    )
    page.text(72, 220, "All fees are payable before the semester begins.")
    return page


def attendance_page():
    page = fx.Page().text(72, 60, "ATTENDANCE SUMMARY", size=14, font="F2")
    for x, label in (
        (72, "Course"),
        (200, "Present"),
        (280, "Total"),
        (340, "Percent"),
    ):
        page.text(x, 100, label)
    rows = [
        ("CS301", "38", "45", "84.4"),
        ("CS302", "31", "45", "68.9"),
        ("CS303", "40", "45", "88.9"),
    ]
    for index, (course, present, total, percent) in enumerate(rows):
        y = 114 + 14 * index
        page.text(72, y, course)
        page.text(238 - 5.6 * len(present), y, present)
        page.text(315 - 5.6 * len(total), y, total)
        page.text(385 - 5.6 * len(percent), y, percent)
    page.text(
        72, 180, "Note: attendance below 75 percent bars a student from the exam."
    )
    return page


LEFT = [
    "The quick brown fox jumps over the lazy dog and",
    "keeps running through the green field until it",
    "reaches the river where the water is cold and",
    "clear enough to see every stone on the bed.",
]
RIGHT = [
    "A second column carries a different story about",
    "a library that opens early in the morning and",
    "closes late at night during the examination",
    "season when students need a quiet place.",
]


def newsletter_page():
    page = fx.Page().text(72, 60, "Newsletter", size=14, font="F2")
    for index, (a, b) in enumerate(zip(LEFT, RIGHT, strict=True)):
        page.text(72, 100 + 14 * index, a)
        page.text(320, 100 + 14 * index, b)
    return page


class RuledTableTests(SimpleTestCase):
    def test_blank_and_merged_cells_stay_where_they_are(self):
        (result,) = run(fee_page())

        self.assertEqual(result.extractor, "pdfplumber")
        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(
            table.rows,
            [
                ["Programme", "Tuition", "Exam Fee"],
                ["B.Tech CSE", "85000", "2500"],
                ["B.Tech ECE", "", "2500"],
                ["Hostel (merged)", "", ""],
            ],
        )
        self.assertEqual(table.source, "pdfplumber:ruled")
        self.assertEqual(table.header, ["Programme", "Tuition", "Exam Fee"])

    def test_the_old_extractor_put_the_number_in_the_wrong_column(self):
        """The reason this exists: with pypdf, a blank cell leaves no trace,
        so "B.Tech ECE" is followed directly by "2500"."""
        (result,) = run(fee_page())

        self.assertIn("B.Tech ECE |  | 2500", result.text)

    def test_the_title_and_the_paragraph_are_text_around_the_table(self):
        (result,) = run(fee_page())

        table = result.tables[0]
        before = result.text[: table.character_start]
        after = result.text[table.character_end :]
        self.assertEqual(before.strip(), "FEE STRUCTURE 2024")
        self.assertEqual(
            after.strip(), "All fees are payable before the semester begins."
        )

    def test_the_stored_span_verifies_cell_for_cell(self):
        (result,) = run(fee_page())
        table = result.tables[0]

        detected = detector.DetectedTable(
            page_number=1,
            table_index=0,
            character_start=table.character_start,
            character_end=table.character_end,
            rows=table.rows,
            separator="PIPE",
        )

        self.assertTrue(detector.verify(detected, result.text))

    def test_a_box_around_a_paragraph_is_not_a_table(self):
        page = fx.Page().box(72, 100, 300, 60)
        page.text(80, 110, "Boxed notice about the library timings")
        page.text(80, 124, "that continue on a second line")

        (result,) = run(page)

        self.assertEqual(result.tables, [])
        self.assertIn("Boxed notice about the library timings", result.text)

    def test_too_many_rulings_skip_ruled_detection_but_keep_the_text(self):
        with mock.patch.object(pdf, "MAX_RULING_OBJECTS", 1):
            (result,) = run(fee_page())

        self.assertIn("ruled_tables_skipped", result.metadata)
        self.assertNotIn("pdfplumber:ruled", [t.source for t in result.tables])
        self.assertIn("85000", result.text)


class LayoutTests(SimpleTestCase):
    def test_a_borderless_table_is_read_as_rows_and_columns(self):
        (result,) = run(attendance_page())

        self.assertEqual(len(result.tables), 1)
        table = result.tables[0]
        self.assertEqual(table.source, "pdfplumber:layout")
        self.assertEqual(
            table.rows,
            [
                ["Course", "Present", "Total", "Percent"],
                ["CS301", "38", "45", "84.4"],
                ["CS302", "31", "45", "68.9"],
                ["CS303", "40", "45", "88.9"],
            ],
        )
        self.assertEqual(
            slice_of(result, table).split("\n")[1], "CS301 | 38 | 45 | 84.4"
        )

    def test_the_note_under_a_borderless_table_stays_text(self):
        (result,) = run(attendance_page())

        table = result.tables[0]
        self.assertIn(
            "Note: attendance below 75 percent",
            result.text[table.character_end :],
        )
        self.assertNotIn("Note", slice_of(result, table))

    def test_two_columns_are_read_one_after_the_other(self):
        (result,) = run(newsletter_page())

        self.assertEqual(result.tables, [])
        left_end = result.text.index(LEFT[-1])
        right_start = result.text.index(RIGHT[0])
        self.assertLess(left_end, right_start)
        # The old reading: a line of each, alternating.
        self.assertNotIn("dog and A second column", result.text)
        for line in LEFT + RIGHT:
            self.assertIn(line, result.text)

    def test_plain_prose_comes_out_as_lines_with_no_tables(self):
        page = fx.Page().lines(
            72,
            100,
            [
                "The committee met on Tuesday and reviewed the policy.",
                "The registrar noted that the rule had not changed.",
            ],
        )

        (result,) = run(page)

        self.assertEqual(result.tables, [])
        self.assertEqual(
            result.text,
            "The committee met on Tuesday and reviewed the policy.\n"
            "The registrar noted that the rule had not changed.",
        )
        self.assertFalse(result.needs_ocr)


class UnusableTextLayerTests(SimpleTestCase):
    def test_unmapped_glyphs_defer_the_page_to_ocr(self):
        junk = "".join(f"(cid:{n})" for n in range(10, 40))
        page = fx.Page().text(72, 100, junk)

        (result,) = run(page)

        self.assertTrue(result.needs_ocr)
        self.assertEqual(result.text, "")
        self.assertIn("unmapped glyphs", result.metadata["ocr_reason"])

    def test_a_page_with_no_text_is_deferred_to_ocr(self):
        (result,) = run(fx.Page())

        self.assertTrue(result.needs_ocr)
        self.assertEqual(result.metadata["ocr_reason"], "no text layer")

    def test_replacement_and_private_use_characters_are_unreadable(self):
        replaced = [geometry.Word("\ufffd" * 12, 0, 10, 0, 10)]
        private = [geometry.Word("\ue001" * 12, 0, 10, 0, 10)]
        fine = [geometry.Word("Registrar of examinations", 0, 10, 0, 10)]

        self.assertIn("replacement", pdf._garbled(replaced, []))
        self.assertIn("private-use", pdf._garbled(private, []))
        self.assertEqual(pdf._garbled(fine, []), "")

    def test_a_page_whose_only_text_is_unmapped_glyphs_is_blank_not_four_glyphs(self):
        """Four (cid:0) is what a NUL-only text layer looks like to pdfminer.
        Too few glyphs for a share to mean anything - but nothing readable is
        left, so it must go to OCR rather than be stored as "text"."""
        junk = [geometry.Word("(cid:0)(cid:0)(cid:0)(cid:0)", 0, 10, 0, 10)]

        self.assertIn("unmapped", pdf._garbled(junk, []))

    def test_a_few_stray_glyphs_do_not_condemn_a_page(self):
        words = [geometry.Word("(cid:5) is the registrar of the college", 0, 10, 0, 10)]

        self.assertEqual(pdf._garbled(words, []), "")


class PictureTests(SimpleTestCase):
    def picture_page(self, *, text_over_it=False):
        page = fx.Page().text(72, 60, "Scanned insert follows", size=12)
        page.image(72, 100, 400, 200, Image.new("RGB", (400, 200), "white"))
        if text_over_it:
            for index in range(6):
                page.text(80, 110 + 14 * index, f"word{index} word{index}b")
        return page

    def test_a_large_picture_with_no_text_over_it_is_read_and_placed(self):
        seen = []

        def read(image):
            seen.append(image.size)
            return [
                geometry.Word("Scanned", 10, 70, 10, 30),
                geometry.Word("table", 80, 130, 10, 30),
            ]

        (result,) = run(self.picture_page(), region_ocr=read, max_regions=5, dpi=72)

        self.assertEqual(len(seen), 1)
        self.assertEqual(result.metadata["pictures_ocred"], 1)
        self.assertIn("Scanned table", result.text)
        self.assertLess(
            result.text.index("Scanned insert follows"),
            result.text.index("Scanned table"),
        )

    def test_a_picture_that_already_has_a_text_layer_is_left_alone(self):
        read = mock.Mock(return_value=[])

        (result,) = run(
            self.picture_page(text_over_it=True),
            region_ocr=read,
            max_regions=5,
            dpi=72,
        )

        read.assert_not_called()
        self.assertNotIn("pictures_ocred", result.metadata)

    def test_the_picture_budget_is_a_hard_limit(self):
        read = mock.Mock(return_value=[])

        (result,) = run(self.picture_page(), region_ocr=read, max_regions=0, dpi=72)

        read.assert_not_called()

    def test_without_a_reader_pictures_are_ignored(self):
        (result,) = run(self.picture_page())

        self.assertEqual(result.text, "Scanned insert follows")


class FallbackTests(SimpleTestCase):
    def test_one_failing_page_falls_back_to_the_old_extractor(self):
        real = pdf._extract_page
        calls = {"n": 0}

        def flaky(page, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("malformed content stream")
            return real(page, *args, **kwargs)

        pages = [
            fx.Page().text(72, 100, "First page text"),
            fx.Page().text(72, 100, "Second page text"),
            fx.Page().text(72, 100, "Third page text"),
        ]

        with mock.patch.object(pdf, "_extract_page", flaky):
            results = run(*pages)

        self.assertEqual(
            [r.extractor for r in results], ["pdfplumber", "pypdf", "pdfplumber"]
        )
        self.assertEqual(results[1].text, "Second page text")
        self.assertIn(
            "malformed content stream", results[1].metadata["fallback_reason"]
        )
        self.assertEqual(results[2].text, "Third page text")

    def test_a_file_pdfplumber_cannot_open_is_still_a_clean_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "broken.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4\nthis is not really a pdf")

            with self.assertRaises(StageError) as caught:
                pdf.extract_page_results(path)

        self.assertEqual(caught.exception.code, ErrorCode.CORRUPT_DOCUMENT)

    def test_extract_pages_still_returns_plain_strings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "two.pdf")
            fx.write(
                path,
                [
                    fx.Page().text(72, 100, "Alpha page"),
                    fx.Page().text(72, 100, "Beta page"),
                ],
            )

            self.assertEqual(pdf.extract_pages(path), ["Alpha page", "Beta page"])
            self.assertEqual(pdf.page_count(path), 2)
