"""PowerPoint decks: slides as pages, with tables, charts and notes.

Decks are built with python-pptx in the test. The cases are the things a
slide holds that its plain text does not: the order the shapes READ in (not
the order they were added), a table with a merged cell, a chart whose numbers
live in a hidden workbook, speaker notes, and numbered bullets.
"""

import io
import os
import tempfile
import zipfile
from unittest import mock

from django.test import SimpleTestCase
from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import presentation
from documents.processing.extractors.ocr import OcrPage

A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def deck_pages(prs, **options):
    buffer = io.BytesIO()
    prs.save(buffer)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pptx")
        with open(path, "wb") as handle:
            handle.write(buffer.getvalue())
        return presentation.extract_slides(path, **options)


def titled(prs, title):
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    slide.shapes.title.text = title
    return slide


def textbox(slide, text, left, top, width=3, height=1):
    box = slide.shapes.add_textbox(
        Inches(left), Inches(top), Inches(width), Inches(height)
    )
    box.text_frame.text = text
    return box


class SlideTextTests(SimpleTestCase):
    def test_each_slide_is_a_page_in_order_with_the_title_first(self):
        prs = Presentation()
        first = prs.slides.add_slide(prs.slide_layouts[1])
        first.shapes.title.text = "Admissions"
        first.placeholders[1].text_frame.text = "Applications open in June"
        titled(prs, "Fees")

        pages, metadata = deck_pages(prs)

        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].text, "Admissions\nApplications open in June")
        self.assertEqual(pages[1].text, "Fees")
        self.assertEqual(metadata["slides"], 2)
        self.assertEqual([p.metadata["slide"] for p in pages], [1, 2])

    def test_shapes_read_top_to_bottom_then_left_to_right(self):
        prs = Presentation()
        slide = titled(prs, "Timetable")
        # Added in the "wrong" order on purpose.
        textbox(slide, "bottom", left=1, top=5)
        textbox(slide, "right of top", left=5, top=2.0)
        textbox(slide, "left of top", left=1, top=2.1)

        (page,), _ = deck_pages(prs)

        self.assertEqual(
            page.text.split("\n"),
            ["Timetable", "left of top", "right of top", "bottom"],
        )

    def test_the_title_comes_first_even_when_it_sits_lower_on_the_slide(self):
        prs = Presentation()
        slide = titled(prs, "Heading")
        slide.shapes.title.top = Inches(6)
        textbox(slide, "body text", left=1, top=1)

        (page,), _ = deck_pages(prs)

        self.assertEqual(page.text.split("\n"), ["Heading", "body text"])

    def test_notes_are_appended_after_the_slide(self):
        prs = Presentation()
        slide = titled(prs, "Exams")
        slide.notes_slide.notes_text_frame.text = "Remind them about the hall tickets."

        (page,), metadata = deck_pages(prs)

        self.assertEqual(page.text, "Exams\nNotes: Remind them about the hall tickets.")
        self.assertEqual(metadata["notes"], 1)

    def test_a_hidden_slide_is_read_and_marked(self):
        prs = Presentation()
        slide = titled(prs, "Draft")
        slide._element.set("show", "0")

        (page,), _ = deck_pages(prs)

        self.assertEqual(page.text, "Draft")
        self.assertTrue(page.metadata["hidden"])

    def test_grouped_shapes_are_read_in_place(self):
        prs = Presentation()
        slide = titled(prs, "Org chart")
        group = slide.shapes.add_group_shape()
        for index, name in enumerate(["Dean", "Registrar"]):
            box = group.shapes.add_textbox(
                Inches(1 + index * 3), Inches(2), Inches(2), Inches(1)
            )
            box.text_frame.text = name

        (page,), _ = deck_pages(prs)

        self.assertEqual(page.text.split("\n"), ["Org chart", "Dean", "Registrar"])


class BulletTests(SimpleTestCase):
    def paragraph_with(self, frame, text, level, bullet_xml=None):
        paragraph = frame.add_paragraph()
        paragraph.text = text
        paragraph.level = level
        if bullet_xml:
            properties = paragraph._p.get_or_add_pPr()
            properties.append(etree.fromstring(bullet_xml))
        return paragraph

    def test_only_bullets_the_slide_itself_asks_for_are_written(self):
        prs = Presentation()
        slide = titled(prs, "Rules")
        box = textbox(slide, "plain line", left=1, top=2)
        self.paragraph_with(
            box.text_frame, "bulleted", 0, f'<a:buChar xmlns:a="{A}" char="x"/>'
        )
        self.paragraph_with(
            box.text_frame, "nested bullet", 1, f'<a:buChar xmlns:a="{A}" char="-"/>'
        )

        (page,), _ = deck_pages(prs)

        self.assertEqual(
            page.text.split("\n"),
            ["Rules", "plain line", "• bulleted", "  - nested bullet"],
        )

    def test_auto_numbered_lists_are_counted_and_restart_after_a_break(self):
        prs = Presentation()
        slide = titled(prs, "Steps")
        box = textbox(slide, "intro", left=1, top=2)
        numbered = f'<a:buAutoNum xmlns:a="{A}" type="arabicPeriod"/>'
        roman = f'<a:buAutoNum xmlns:a="{A}" type="romanLcParenR"/>'
        self.paragraph_with(box.text_frame, "first", 0, numbered)
        self.paragraph_with(box.text_frame, "second", 0, numbered)
        self.paragraph_with(box.text_frame, "sub one", 1, roman)
        self.paragraph_with(box.text_frame, "sub two", 1, roman)
        self.paragraph_with(box.text_frame, "interrupt", 0)
        self.paragraph_with(box.text_frame, "again", 0, numbered)

        (page,), _ = deck_pages(prs)

        self.assertEqual(
            page.text.split("\n"),
            [
                "Steps",
                "intro",
                "1. first",
                "2. second",
                "  i) sub one",
                "  ii) sub two",
                "interrupt",
                "1. again",
            ],
        )


class TableAndChartTests(SimpleTestCase):
    def table_slide(self, prs):
        slide = titled(prs, "Fee table")
        shape = slide.shapes.add_table(3, 3, Inches(1), Inches(2), Inches(6), Inches(2))
        table = shape.table
        for r, row in enumerate(
            [["Course", "Fee", ""], ["CS101", "5000", "Lab"], ["CS102", "", "Lecture"]]
        ):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
        return slide, table

    def test_a_slide_table_is_stored_as_cells_with_blanks_kept(self):
        prs = Presentation()
        self.table_slide(prs)

        (page,), metadata = deck_pages(prs)

        self.assertEqual(
            page.tables[0].rows,
            [["Course", "Fee", ""], ["CS101", "5000", "Lab"], ["CS102", "", "Lecture"]],
        )
        self.assertEqual(page.tables[0].source, "python-pptx:table")
        self.assertEqual(metadata["tables"], 1)

    def test_a_merged_cell_holds_its_text_once(self):
        prs = Presentation()
        _, table = self.table_slide(prs)
        table.cell(0, 1).merge(table.cell(0, 2))

        (page,), _ = deck_pages(prs)

        header = page.tables[0].rows[0]
        self.assertEqual(header[0], "Course")
        self.assertEqual(sum(cell != "" for cell in header), 2)

    def test_a_chart_becomes_a_table_of_its_numbers(self):
        prs = Presentation()
        slide = titled(prs, "Placement")
        data = CategoryChartData()
        data.categories = ["2022", "2023"]
        data.add_series("Intake", (120, 150))
        data.add_series("Placed", (100, 140.5))
        slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED,
            Inches(1),
            Inches(2),
            Inches(6),
            Inches(3),
            data,
        )

        (page,), metadata = deck_pages(prs)

        self.assertEqual(
            page.tables[0].rows,
            [
                ["", "Intake", "Placed"],
                ["2022", "120", "100"],
                ["2023", "150", "140.5"],
            ],
        )
        self.assertEqual(page.tables[0].source, "python-pptx:chart")
        self.assertEqual(metadata["charts"], 1)


def picture_png(text="INVOICE 12345"):
    image = Image.new("RGB", (800, 220), "white")
    ImageDraw.Draw(image).text(
        (30, 70), text, fill="black", font=ImageFont.load_default(size=56)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class PictureTests(SimpleTestCase):
    def deck(self, copies=1):
        prs = Presentation()
        slide = titled(prs, "Scanned notice")
        for index in range(copies):
            slide.shapes.add_picture(
                io.BytesIO(picture_png()), Inches(1), Inches(2 + index), Inches(6)
            )
        return prs

    def reader(self):
        return mock.Mock(
            return_value=OcrPage(
                text="Registrar of examinations", words=6, confidence=90.0
            )
        )

    def test_a_picture_on_a_slide_is_read(self):
        reader = self.reader()

        (page,), metadata = deck_pages(self.deck(), image_ocr=reader, max_images=3)

        self.assertEqual(page.text, "Scanned notice\nRegistrar of examinations")
        self.assertEqual(metadata["pictures_ocred"], 1)

    def test_the_budget_is_a_hard_limit(self):
        reader = self.reader()

        deck_pages(self.deck(), image_ocr=reader, max_images=0)

        reader.assert_not_called()

    def test_the_same_crest_on_every_slide_is_read_once(self):
        reader = self.reader()

        deck_pages(self.deck(copies=3), image_ocr=reader, max_images=5)

        self.assertEqual(reader.call_count, 1)


class FileTests(SimpleTestCase):
    def test_slides_are_counted_from_part_names_without_parsing(self):
        prs = Presentation()
        for number in range(3):
            titled(prs, f"Slide {number}")
        buffer = io.BytesIO()
        prs.save(buffer)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "deck.pptx")
            with open(path, "wb") as handle:
                handle.write(buffer.getvalue())

            self.assertEqual(presentation.slide_count(path), 3)
            self.assertTrue(presentation.is_pptx(path))

    def test_an_unreadable_deck_is_a_corrupt_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bad.pptx")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/presentation.xml", "not xml at all")

            with self.assertRaises(StageError) as caught:
                presentation.extract_slides(path)

        self.assertEqual(caught.exception.code, ErrorCode.CORRUPT_DOCUMENT)
