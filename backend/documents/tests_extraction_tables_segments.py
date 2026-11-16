"""Where one ruled grid holds several tables, and which rows are no table at all.

Two tables drawn touching are one grid to pdfplumber and are read apart where
the columns change for good. A Word table edited row by row is still one
table: its borders drift a few points from row to row. A box holding a table
of its own is left to the layout pass - but only when the whole row is such
boxes; a cell whose text runs in two columns beside ordinary cells, or a
list in a cell, is a cell.
"""

import os
import tempfile

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import pdf

COURSES = [
    ["S.No", "Course Title", "Credits"],
    ["1", "Engineering Mathematics", "4"],
    ["2", "Engineering Physics", "3"],
    ["3", "Basic Electrical Engg", "3"],
    ["4", "Programming in C", "4"],
    ["5", "Engineering Graphics", "2"],
]


def run(page):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, [page])
        (result,) = pdf.extract_page_results(path)
        return result


def ruled(result):
    return [t.rows for t in result.tables if t.source == pdf.SOURCE_RULED]


def drawn_row_by_row(rows, edges_of, top=100, height=20):
    """A table whose every row is drawn with its own borders, as Word draws a
    table whose rows were resized one by one. `edges_of(index)` gives that
    row's column boundaries."""
    page = fx.Page()
    for index, row in enumerate(rows):
        edges = edges_of(index)
        y = top + index * height
        page.rule(edges[0], y, edges[-1], y)
        page.rule(edges[0], y + height, edges[-1], y + height)
        for x in edges:
            page.rule(x, y, x, y + height)
        for x, cell in zip(edges, row, strict=False):
            page.text(x + 4, y + 5, cell)
    return page


EDGES = [72, 112, 312, 372]


def moved(rows, at, by):
    """Row boundaries with boundary `at` of the rows in `rows` moved `by`."""

    def edges_of(index):
        edges = list(EDGES)
        if index in rows:
            edges[at] += by
        return edges

    return edges_of


class JaggedRowTests(SimpleTestCase):
    def test_a_header_row_a_few_points_short_stays_with_its_body(self):
        (table,) = ruled(run(drawn_row_by_row(COURSES, moved({0}, 3, -7))))

        self.assertEqual(table, COURSES)

    def test_rows_a_few_points_wider_do_not_split_the_table(self):
        (table,) = ruled(run(drawn_row_by_row(COURSES, moved({3, 4}, 3, 5))))

        self.assertEqual(table, COURSES)

    def test_an_inner_border_dragged_a_few_points_makes_no_column(self):
        (table,) = ruled(run(drawn_row_by_row(COURSES, moved({3}, 2, 5))))

        self.assertEqual(table, COURSES)

    def test_one_row_with_columns_of_its_own_does_not_split_the_table(self):
        """Row 3's inner border sits 20 points out: a new boundary, but the
        rows under it go back to the table's columns."""
        (table,) = ruled(run(drawn_row_by_row(COURSES, moved({3}, 2, 20))))

        self.assertEqual([row[0] for row in table], [row[0] for row in COURSES])

    def test_a_first_row_is_never_cut_off_from_the_rows_under_it(self):
        """A header with columns of its own over a body that subdivides it
        differently is still the body's header."""
        page = fx.Page()
        for y in (100, 120, 140, 160):
            page.rule(72, y, 372, y)
        for x in (72, 372):
            page.rule(x, 100, x, 160)
        page.rule(222, 100, 222, 120)
        for x in (172, 272):
            page.rule(x, 120, x, 160)
        page.text(76, 105, "Course")
        page.text(226, 105, "Marks")
        for y, cells in ((125, ["DBMS", "30", "70"]), (145, ["Networks", "25", "75"])):
            for x, cell in zip((76, 176, 276), cells, strict=True):
                page.text(x, y, cell)

        tables = ruled(run(page))

        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]), 3)

    def test_touching_tables_whose_first_columns_nearly_meet_are_two(self):
        """Course outcomes over a CO-PO matrix: the matrix's first column
        ends 7 points left of the outcomes' first column, and every other
        boundary of it is new."""
        page = fx.Page()
        outcomes = [["CO-1", "Explain research types"], ["CO-2", "Design a study"]]
        matrix = [
            ["PO CO", "PO1", "PO2", "PO3"],
            ["CO-1", "3", "2", "1"],
            ["CO-2", "2", "3", "1"],
        ]
        y = 100
        for row in outcomes:
            page.rule(72, y, 400, y)
            for x in (72, 120, 400):
                page.rule(x, y, x, y + 20)
            page.text(76, y + 5, row[0])
            page.text(124, y + 5, row[1])
            y += 20
        for row in matrix:
            page.rule(72, y, 400, y)
            for x in (72, 113, 180, 290, 400):
                page.rule(x, y, x, y + 20)
            for x, cell in zip((76, 117, 184, 294), row, strict=True):
                page.text(x, y + 5, cell)
            y += 20
        page.rule(72, y, 400, y)

        self.assertEqual(ruled(run(page)), [outcomes, matrix])


# -- boxes holding tables of their own ---------------------------------------


def three_column_cell_row(lines, x=172, right=472, size=9, gap=40):
    """A row whose wide cell holds `lines` of (left, right) text pairs."""
    page = fx.Page()
    for y in (100, 120, 180, 200):
        page.rule(72, y, right, y)
    for edge in (72, x, right):
        page.rule(edge, 100, edge, 200)
    page.text(76, 105, "Area")
    page.text(x + 4, 105, "Details")
    page.text(76, 125, "Item")
    for index, (left, other) in enumerate(lines):
        y = 124 + 15 * index
        page.text(x + 4, y, left, size=size)
        if other:
            page.text(x + 4 + gap + 60, y, other, size=size)
    page.text(76, 185, "Next")
    page.text(x + 4, 185, "Another row")
    return page


class ContainerTests(SimpleTestCase):
    def test_details_in_two_columns_beside_other_cells_stay_a_cell(self):
        page = three_column_cell_row(
            [
                ("Laptop 15", ""),
                ("Serial: DL15", "Warranty: 1 year"),
                ("Colour: Black", "HSN: 8471"),
            ]
        )

        (table,) = ruled(run(page))

        self.assertEqual(len(table), 3)
        self.assertEqual(table[1][0], "Item")
        self.assertIn("Warranty: 1 year", table[1][1])

    def test_a_box_alone_in_its_row_holding_a_table_is_left_to_the_layout(self):
        page = fx.Page()
        page.box(72, 100, 400, 20)
        page.rule(272, 100, 272, 120)
        page.text(76, 105, "Office")
        page.text(276, 105, "Votes")
        page.box(72, 120, 200, 60)
        page.box(272, 120, 200, 60)
        for index, (name, party, votes) in enumerate(
            (
                ("NEEL KASHKARI", "REP", "247"),
                ("EDMUND BROWN", "DEM", "69"),
                ("TED GAINES", "REP", "249"),
            )
        ):
            y = 124 + 14 * index
            page.text(76, y, name, size=8)
            page.text(176, y, party, size=8)
            page.text(236, y, votes, size=8)
            page.text(276, y, "YES" if index % 2 else "NO", size=8)
            page.text(440, y, str(90 + index), size=8)

        result = run(page)

        line = next(line for line in result.text.splitlines() if "KASHKARI" in line)
        self.assertNotIn("BROWN", line)
        self.assertNotIn(
            "KASHKARI BROWN", " ".join(c for t in ruled(result) for r in t for c in r)
        )

    def test_a_numbered_list_in_a_box_is_a_cell(self):
        """(ii) and (iii) are two letters each: list markers, not a column."""
        page = fx.Page()
        page.box(72, 100, 400, 20)
        page.rule(272, 100, 272, 120)
        page.text(76, 105, "Unit")
        page.text(276, 105, "Hours")
        page.box(72, 120, 400, 50)
        for index, (marker, item) in enumerate(
            (
                ("(i)", "Relational model"),
                ("(ii)", "Keys and constraints"),
                ("(iii)", "Relational algebra"),
            )
        ):
            page.text(80, 124 + 14 * index, marker, size=9)
            page.text(110, 124 + 14 * index, item, size=9)

        (table,) = ruled(run(page))

        self.assertEqual(
            table[1][0],
            "(i) Relational model (ii) Keys and constraints (iii) Relational algebra",
        )

    def test_a_list_numbered_without_stops_in_a_box_is_a_cell(self):
        page = fx.Page()
        page.box(72, 100, 400, 20)
        page.rule(272, 100, 272, 120)
        page.text(76, 105, "Unit")
        page.text(276, 105, "Hours")
        page.box(72, 120, 400, 50)
        for index, item in enumerate(
            ("Relational model", "Keys and constraints", "Relational algebra")
        ):
            page.text(80, 124 + 14 * index, str(index + 1), size=9)
            page.text(110, 124 + 14 * index, item, size=9)

        (table,) = ruled(run(page))

        self.assertIn("1 Relational model", table[1][0])

    def test_a_merged_cell_reaching_into_a_box_row_is_read_once(self):
        """ "GOVERNOR" sits beside a box merged over two rows; the second row
        is only the box. Cut at that row, the box would be read twice."""
        page = fx.Page()
        for y in (100, 120):
            page.rule(72, y, 472, y)
        page.rule(72, 190, 472, 190)
        page.rule(72, 150, 172, 150)
        for x in (72, 172, 472):
            page.rule(x, 100, x, 190)
        page.text(76, 105, "Office")
        page.text(176, 105, "Results")
        page.text(76, 125, "GOVERNOR")
        for index, (name, party, votes) in enumerate(
            (
                ("NEEL KASHKARI", "REP", "247"),
                ("EDMUND BROWN", "DEM", "69"),
                ("GAVIN NEWSOM", "DEM", "64"),
                ("RON NEHRING", "REP", "247"),
            )
        ):
            y = 124 + 15 * index
            page.text(176, y, name, size=8)
            page.text(330, y, party, size=8)
            page.text(420, y, votes, size=8)

        result = run(page)

        self.assertEqual(result.text.count("NEHRING"), 1)
        self.assertEqual(result.text.count("KASHKARI"), 1)

    def test_an_empty_merged_column_does_not_take_the_table_with_a_box(self):
        """A blank spacer column merged down the whole table reaches every
        row; it has no text to be read twice, so the box row alone goes."""
        page = fx.Page()
        for y in (100, 120, 140, 160, 220, 240):
            page.rule(92, y, 472, y)
        page.rule(72, 100, 92, 100)
        page.rule(72, 240, 92, 240)
        for x in (72, 92, 472):
            page.rule(x, 100, x, 240)
        for y in (100, 120, 140):
            page.rule(272, y, 272, y + 20)
        page.rule(272, 220, 272, 240)
        for y, cells in (
            (105, ["Code", "Credits"]),
            (125, ["CS101", "4"]),
            (145, ["CS102", "3"]),
            (225, ["CS103", "2"]),
        ):
            page.text(96, y, cells[0])
            page.text(276, y, cells[1])
        for index, (name, votes) in enumerate(
            (("NEEL KASHKARI", "247"), ("EDMUND BROWN", "69"), ("RON NEHRING", "247"))
        ):
            page.text(96, 164 + 15 * index, name, size=8)
            page.text(420, 164 + 15 * index, votes, size=8)

        tables = ruled(run(page))

        self.assertIn([["Code", "Credits"], ["CS101", "4"], ["CS102", "3"]], tables)
