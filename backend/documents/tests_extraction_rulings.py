"""Ruled PDF tables as a reader sees them (extractors/rulings.py).

Each fixture reproduces, in coordinates, a defect found in real uploads: a
Word export whose invisible cell backgrounds were read as rulings (seven
columns for three, one row per wrapped line, "database" cut into "d" and
"atabase"), two tables sharing a border read as one grid, merged cells
whose value reached only their first row, a two-row header, a title row of
blanks, a row label printed sideways read backwards, and boxes that hold a
whole table of their own.
"""

import os
import tempfile

import pdfplumber
from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import pdf, rulings

WHITE = (1, 1, 1)
GREEN = (0.933, 0.969, 0.941)


def run(*pages):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path)


def rows_of(page, source="pdfplumber:ruled"):
    return [table.rows for table in page.tables if table.source == source]


# -- Word's invisible rectangles ----------------------------------------------

COLUMNS = [72, 172, 352, 512]
# Header, a row whose description wraps onto a second line, a one-line row.
ROW_EDGES = [100, 124, 162, 186]
CELLS = [
    [["Command"], ["Description"], ["Syntax"]],
    [["DELETE"], ["Delete records from a", "database table"], ["DELETE FROM t;"]],
    [["LOCK"], ["Table control"], ["LOCK TABLE t;"]],
]


def word_table(colour=WHITE):
    """A table as Word saves it.

    Thin borders; behind every cell a rectangle in the cell's colour; behind
    every line of text a strip the width of the cell, and over it the
    paragraph's own rectangle, inset seven points from the borders. On white
    paper, or on a cell of the same colour, none of them can be seen - but
    the strips' edges reach the borders and the insets' edges cross the
    strips, which is exactly what pdfplumber takes for a grid.
    """
    page = fx.Page()
    for r, row in enumerate(CELLS):
        top, bottom = ROW_EDGES[r], ROW_EDGES[r + 1]
        for c, lines in enumerate(row):
            left, right = COLUMNS[c], COLUMNS[c + 1]
            page.fill(
                left + 0.3, top + 0.3, right - left - 0.6, bottom - top - 0.6, colour
            )
            for index, line in enumerate(lines):
                y = top + 5 + 14 * index
                page.fill(left + 0.3, y - 1, right - left - 0.6, 14, colour)
                page.fill(left + 7, y - 1, right - left - 14, 14, colour)
                page.text(left + 8, y, line)
    for x in COLUMNS:
        page.rule(x, ROW_EDGES[0], x, ROW_EDGES[-1], width=0.5)
    for y in ROW_EDGES:
        page.rule(COLUMNS[0], y, COLUMNS[-1], y, width=0.5)
    return page


EXPECTED = [
    ["Command", "Description", "Syntax"],
    ["DELETE", "Delete records from a database table", "DELETE FROM t;"],
    ["LOCK", "Table control", "LOCK TABLE t;"],
]


class InvisibleRectangleTests(SimpleTestCase):
    def test_white_backgrounds_add_no_columns_and_no_rows(self):
        (page,) = run(word_table())

        self.assertEqual(rows_of(page), [EXPECTED])

    def test_shading_painted_on_the_same_shade_is_not_a_ruling(self):
        (page,) = run(word_table(GREEN))

        self.assertEqual(rows_of(page), [EXPECTED])

    def test_the_backgrounds_are_what_pdfplumber_took_for_rulings(self):
        """Without the filter the same page reads as the broken grid."""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.pdf")
            fx.write(path, [word_table()])
            with pdfplumber.open(path) as document:
                page = document.pages[0]
                raw = page.find_tables(pdf.RULED_SETTINGS)[0].extract()
                seen = rulings.visible_page(page).find_tables(pdf.RULED_SETTINGS)[0]

        self.assertGreater(len(raw[0]), 3)
        self.assertEqual(len(seen.extract()[0]), 3)

    def test_a_coloured_cell_on_white_paper_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.pdf")
            page = fx.Page().fill(72, 100, 100, 20, GREEN).fill(72, 140, 100, 20, WHITE)
            fx.write(path, [page])
            with pdfplumber.open(path) as document:
                plumbed = document.pages[0]
                hidden = rulings.invisible(plumbed)
                green, white = sorted(plumbed.rects, key=lambda r: r["top"])

        self.assertNotIn(id(green), hidden)
        self.assertIn(id(white), hidden)

    def test_colours_compare_across_colour_spaces(self):
        self.assertEqual(rulings._rgb(1.0), (1.0, 1.0, 1.0))
        self.assertEqual(rulings._rgb((0.5,)), (0.5, 0.5, 0.5))
        self.assertEqual(rulings._rgb((0, 0, 0, 0)), (1.0, 1.0, 1.0))
        self.assertEqual(rulings._rgb(None), (0.0, 0.0, 0.0))
        self.assertIsNone(rulings._rgb(("P1",)))
        self.assertTrue(rulings._same((1.0, 1.0, 1.0), rulings._rgb((0, 0, 0, 0))))
        self.assertFalse(rulings._same(None, (1.0, 1.0, 1.0)))


# -- one grid, two tables ----------------------------------------------------


def stacked_tables():
    """Two tables drawn touching: the second starts on the first's last rule."""
    page = fx.Page()
    for y in (100, 120, 140, 160, 180, 200):
        page.rule(72, y, 372, y)
    for x in (72, 172, 272, 372):
        page.rule(x, 100, x, 160)
    for x in (72, 222, 372):
        page.rule(x, 160, x, 200)
    for y, cells in (
        (105, ["Code", "Course", "Credits"]),
        (125, ["CS101", "Programming", "4"]),
        (145, ["CS102", "Databases", "3"]),
    ):
        for x, cell in zip((76, 176, 276), cells, strict=True):
            page.text(x, y, cell)
    for y, cells in ((165, ["Stream", "Elective"]), (185, ["PE-1", "Robotics"])):
        for x, cell in zip((76, 226), cells, strict=True):
            page.text(x, y, cell)
    return page


class StackedTableTests(SimpleTestCase):
    def test_tables_sharing_a_border_are_read_apart(self):
        (page,) = run(stacked_tables())

        self.assertEqual(
            rows_of(page),
            [
                [
                    ["Code", "Course", "Credits"],
                    ["CS101", "Programming", "4"],
                    ["CS102", "Databases", "3"],
                ],
                [["Stream", "Elective"], ["PE-1", "Robotics"]],
            ],
        )


# -- merged cells ------------------------------------------------------------


def pools_table(title="LIST OF POOLS", figures=True):
    """A titled table with a two-row header and values merged down.

    "Pool" and "Credits" are merged down across both header rows,
    "Periods" across L, T and P; in the body "Pool-I" and its 4 credits are
    merged down across two courses.
    """
    page = fx.Page()
    left, right = 72, 432
    page.rule(left, 80, right, 80)
    page.rule(left, 80, left, 100)
    page.rule(right, 80, right, 100)
    page.text(200, 85, title)

    page.rule(left, 100, right, 100)
    page.rule(232, 120, 352, 120)
    page.rule(left, 140, right, 140)
    page.rule(152, 160, 352, 160)
    page.rule(left, 180, right, 180)
    for x in (72, 152, 232, 352, 432):
        page.rule(x, 100, x, 180)
    for x in (272, 312):
        page.rule(x, 120, x, 180)

    page.text(76, 115, "Pool")
    page.text(156, 115, "Course")
    page.text(236, 105, "Periods")
    page.text(236, 125, "L")
    page.text(276, 125, "T")
    page.text(316, 125, "P")
    page.text(356, 115, "Credits")
    page.text(76, 155, "Pool-I")
    page.text(356, 155, "4" if figures else "core")
    for y, cells in (
        (145, ["Robotics", "3", "1", "0"] if figures else ["Robotics", "a", "b", "c"]),
        (165, ["Turbines", "2", "0", "2"] if figures else ["Turbines", "d", "e", "f"]),
    ):
        for x, cell in zip((156, 236, 276, 316), cells, strict=True):
            page.text(x, y, cell)
    return page


HEADER = ["Pool", "Course", "Periods L", "Periods T", "Periods P", "Credits"]


class MergedCellTests(SimpleTestCase):
    def test_a_group_label_merged_down_belongs_to_every_record_it_spans(self):
        (page,) = run(pools_table())

        (table,) = page.tables
        self.assertEqual(table.rows[2][:2], ["Pool-I", "Robotics"])
        self.assertEqual(table.rows[3][:2], ["Pool-I", "Turbines"])

    def test_a_merged_figure_stays_once_where_it_is_printed(self):
        """The pool's 4 credits are one value: written into both courses, a
        sum over the Credits column would count it twice."""
        (page,) = run(pools_table())

        (table,) = page.tables
        self.assertEqual(table.rows[2], ["Pool-I", "Robotics", "3", "1", "0", "4"])
        self.assertEqual(table.rows[3], ["Pool-I", "Turbines", "2", "0", "2", ""])

    def test_a_header_split_over_two_rows_is_one_header(self):
        (page,) = run(pools_table())

        (table,) = page.tables
        self.assertEqual(table.rows[1], HEADER)
        # The header of a stored table is its first row - here the title.
        self.assertEqual(table.header, table.rows[0])
        self.assertEqual(len(table.rows), 4)

    def test_a_title_over_a_header_with_no_figure_under_it_is_no_header(self):
        """Only a first row built as a header is known to be one: under a
        title, the first row is the title, and it is not guessed to be a header
        of a body that says nothing."""
        (page,) = run(pools_table(figures=False))

        (table,) = page.tables
        self.assertEqual(table.rows[0][0], "LIST OF POOLS")
        self.assertEqual(table.rows[1], HEADER)
        self.assertEqual(table.header, [])

    def test_a_full_width_first_row_stays_the_tables_title(self):
        (page,) = run(pools_table())

        (table,) = page.tables
        self.assertEqual(table.rows[0], ["LIST OF POOLS", "", "", "", "", ""])
        self.assertEqual(page.text[: table.character_start].strip(), "")
        span = page.text[table.character_start : table.character_end]
        self.assertTrue(span.startswith("LIST OF POOLS"))

    def test_a_merged_paragraph_stays_in_its_first_row(self):
        self.assertTrue(rulings._is_short("Pool-I (III Year- I Sem)"))
        self.assertFalse(
            rulings._is_short(
                "Apply shear stress principles in thin-walled members and "
                "analyze shear centers in open structures"
            )
        )


# -- what is inside a cell -----------------------------------------------------


class CellContentTests(SimpleTestCase):
    def test_a_row_label_printed_sideways_reads_forwards(self):
        page = fx.Page()
        edges = [100, 120, 180, 240]
        for y in edges:
            page.rule(72, y, 272, y)
        for x in (72, 132, 272):
            page.rule(x, 100, x, 240)
        page.text(76, 105, "Sign")
        page.text(136, 105, "Value")
        page.stamp(100, 175, "Kalemia", angle=90)
        page.text(136, 145, "Low")
        page.stamp(100, 235, "Urine", angle=90)
        page.text(136, 205, "High")

        (result,) = run(page)

        (rows,) = rows_of(result)
        self.assertEqual([row[0] for row in rows], ["Sign", "Kalemia", "Urine"])

    def test_a_box_holding_a_table_of_its_own_is_left_to_the_layout_pass(self):
        """A ruled header over one big box: the box's rows are not cells."""
        page = fx.Page()
        page.box(72, 100, 360, 20)
        page.rule(192, 100, 192, 120)
        page.rule(312, 100, 312, 120)
        page.box(72, 120, 360, 70)
        for x, label in ((76, "NAME"), (196, "PARTY"), (316, "VOTES")):
            page.text(x, 105, label)
        for index, (name, party, votes) in enumerate(
            (
                ("NEEL KASHKARI", "REP", "247"),
                ("EDMUND BROWN", "DEM", "69"),
                ("GAVIN NEWSOM", "DEM", "64"),
                ("RON NEHRING", "REP", "247"),
            )
        ):
            y = 125 + 14 * index
            page.text(76, y, name)
            page.text(196, y, party)
            page.text(316, y, votes)

        (result,) = run(page)

        flattened = [
            cell
            for rows in rows_of(result)
            for row in rows
            for cell in row
            if "KASHKARI" in cell and "BROWN" in cell
        ]
        self.assertEqual(flattened, [])
        line = next(line for line in result.text.splitlines() if "KASHKARI" in line)
        self.assertIn("247", line)
        self.assertNotIn("BROWN", line)


class BandedRowTests(SimpleTestCase):
    def test_wrapped_descriptions_beside_a_short_key_are_whole_rows(self):
        rows = [
            ["Command", "Description", "Syntax"],
            [
                "GRANT",
                "Assigns new privileges\nto a user account,\nallowing access\nto objects.",
                "GRANT privilege\n[(columns)] ON\nobject TO\nuser;",
            ],
            [
                "REVOKE",
                "Removes privileges\nfrom a user account,\ntaking away\naccess.",
                "REVOKE privilege\n[(columns)] ON\nobject FROM\nuser;",
            ],
        ]

        self.assertTrue(pdf._rows_are_whole(rows))

    def test_stacks_of_figures_are_rows_the_rulings_missed(self):
        rows = [
            ["Group", "Route", "Pax", "Load"],
            [
                "JAL",
                "Europe\nKorea\nChina",
                "42,097\n47,126\n97,180",
                "62.0\n79.9\n58.9",
            ],
        ]

        self.assertFalse(pdf._rows_are_whole(rows))
