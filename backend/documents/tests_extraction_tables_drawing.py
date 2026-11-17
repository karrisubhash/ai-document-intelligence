"""What a ruled PDF table is drawn with: which shapes are rulings, which rows whole.

Only a rectangle filled and not outlined, in the colour behind it, is taken
away before the table finder runs - Word's cell and paragraph backgrounds. A
line or an outlined rectangle stays a ruling whatever its colour: a black
rule on a black header bar, a rule in a spot colour (which arrives as a bare
tint, 1.0, and reads as white), an outline over its own fill. A table ruled
only every few rows, whose cells hold several records each, is not stored as
a table - also when the stacks in a band are of different heights.
"""

import os
import tempfile
from unittest import mock

import pdfplumber
from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import pdf, rulings

WHITE = (1, 1, 1)
GREY = (0.8, 0.8, 0.8)
CARD = (0.85, 0.92, 1.0)

ROWS = [
    ["Roll No", "Name", "Branch", "CGPA"],
    ["21A501", "Asha Rao", "CSE", "8.91"],
    ["21A502", "Ravi Kumar", "ECE", "7.45"],
    ["21A503", "Sita Devi", "MECH", "9.02"],
]
COLUMNS = [72, 162, 292, 382, 452]


def run(page):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, [page])
        (result,) = pdf.extract_page_results(path)
        return result


def ruled(result):
    return [t for t in result.tables if t.source == pdf.SOURCE_RULED]


def write_rows(page, top=100, height=20):
    for r, row in enumerate(ROWS):
        for x, cell in zip(COLUMNS, row, strict=False):
            page.text(x + 4, top + r * height + 5, cell)
    return page


def ruled_rows(page, colour=None, top=100, height=20):
    """ROWS with every boundary drawn as a line; the vertical ones row by
    row, as a table drawn cell by cell has them."""
    for r in range(len(ROWS) + 1):
        page.rule(
            COLUMNS[0], top + r * height, COLUMNS[-1], top + r * height, colour=colour
        )
    for r in range(len(ROWS)):
        for x in COLUMNS:
            page.rule(x, top + r * height, x, top + (r + 1) * height, colour=colour)
    return write_rows(page, top, height)


class StrokesStayRulingsTests(SimpleTestCase):
    def test_rules_in_the_colour_of_a_header_bar_still_divide_it(self):
        page = fx.Page().fill(COLUMNS[0], 100, COLUMNS[-1] - COLUMNS[0], 20, (0, 0, 0))
        ruled_rows(page)

        (table,) = ruled(run(page))

        self.assertEqual(table.header, ROWS[0])
        self.assertEqual(table.rows, ROWS)

    def test_rules_in_a_spot_colour_are_rulings(self):
        page = fx.Page(resources=fx.SPOT_COLOUR)
        ruled_rows(page, colour="/CS0 CS 1 SCN")

        (table,) = ruled(run(page))

        self.assertEqual(table.rows, ROWS)

    def test_an_outlined_rectangle_is_a_ruling_whatever_its_colours(self):
        page = fx.Page()
        for r in range(len(ROWS)):
            for left, right in zip(COLUMNS, COLUMNS[1:], strict=False):
                page.rect(
                    left, 100 + 20 * r, right - left, 20, fill=WHITE, stroke=WHITE
                )
        write_rows(page)

        (table,) = ruled(run(page))

        self.assertEqual(table.rows, ROWS)


class HiddenFillTests(SimpleTestCase):
    def hidden(self, page):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.pdf")
            fx.write(path, [page])
            with pdfplumber.open(path) as document:
                plumbed = document.pages[0]
                hidden = rulings.invisible(plumbed)
                return [id(rect) in hidden for rect in plumbed.rects]

    def test_a_fill_is_compared_with_the_smallest_fill_around_it(self):
        page = (
            fx.Page()
            .fill(60, 80, 400, 300, GREY)
            .fill(100, 100, 200, 100, WHITE)
            .fill(110, 110, 50, 20, WHITE)
            .fill(110, 150, 50, 20, GREY)
            .fill(320, 100, 100, 50, GREY)
        )

        self.assertEqual(self.hidden(page), [False, False, True, False, True])

    def test_a_fill_anywhere_on_a_page_wide_fill_is_found_on_it(self):
        page = fx.Page().fill(0, 0, fx.PAGE_WIDTH, fx.PAGE_HEIGHT, GREY)
        spots = [
            (0, 0),
            (37.25, 48.5),
            (38.25, 49.5),
            (300, 400),
            (565, 740),
            (600, 780),
        ]
        for x, y in spots:
            page.fill(x, y, 10, 10, GREY)

        self.assertEqual(self.hidden(page), [False] + [True] * len(spots))

    def test_white_rules_on_a_coloured_card_are_seen_on_a_page_of_many_fills(self):
        """Past 400 fills the old search compared every shape with white
        paper, and the card's white rules vanished."""
        page = fx.Page().fill(60, 80, 420, 140, CARD)
        top, height = 100, 20
        for r in range(len(ROWS) + 1):
            page.fill(
                COLUMNS[0], top + r * height - 0.5, COLUMNS[-1] - COLUMNS[0], 1, WHITE
            )
        for x in COLUMNS:
            page.fill(x - 0.5, top, 1, height * len(ROWS), WHITE)
        write_rows(page, top, height)
        for index in range(420):
            page.fill(60 + (index % 42) * 12, 600 + (index // 42) * 12, 6, 6, GREY)

        (table,) = ruled(run(page))

        self.assertEqual(table.rows, ROWS)

    def test_the_search_looks_at_the_fills_near_a_shape_only(self):
        """A page of 2,400 small shapes on 400 fills: the old search tried
        every fill against every shape, a million comparisons."""
        page = fx.Page()
        for index in range(400):
            page.fill(20 + (index % 20) * 28, 20 + (index // 20) * 38, 26, 36, GREY)
        for index in range(2000):
            page.fill(22 + (index % 40) * 14, 22 + (index // 40) * 15, 4, 4, WHITE)

        with mock.patch.object(rulings, "_contains", wraps=rulings._contains) as spy:
            self.hidden(page)

        self.assertLess(spy.call_count, 50_000)


# -- rows the rulings did not separate ----------------------------------------


def banded(bands):
    """A Name | Amount list ruled under its header and after every band."""
    page = fx.Page()
    page.text(76, 105, "Name")
    page.text(276, 105, "Amount")
    top = 120
    lines_at = [100, top]
    for names, amounts in bands:
        for index, name in enumerate(names):
            page.text(76, top + 4 + 13 * index, name)
        for index, amount in enumerate(amounts):
            if amount:
                page.text(276, top + 4 + 13 * index, amount)
        top += 13 * len(names) + 8
        lines_at.append(top)
    for y in lines_at:
        page.rule(72, y, 372, y)
    for x in (72, 272, 372):
        page.rule(x, 100, x, top)
    return page


class BandedRowTests(SimpleTestCase):
    def test_a_band_whose_stacks_differ_in_height_is_not_a_row(self):
        """One name wraps onto a second line, so the band's names are four
        lines and its amounts three."""
        result = run(
            banded(
                [
                    (
                        ["Asha Rao", "Ravi Kumar Venkata", "Subramanyam", "Kiran"],
                        ["1,200", "2,350", "", "3,100"],
                    ),
                    (
                        ["Mohan Das", "Sita Lakshmi", "Narayana Murthy", "Gopal"],
                        ["4,000", "520", "", "75"],
                    ),
                ]
            )
        )

        self.assertEqual(ruled(result), [])
        self.assertEqual(result.metadata.get("ruled_tables_distrusted"), 1)
        line = next(line for line in result.text.splitlines() if "Asha Rao" in line)
        self.assertNotIn("Ravi", line)
