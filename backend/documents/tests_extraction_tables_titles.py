"""A ruled table's title row: it stays in the table, the header is found under it.

Indian syllabi draw each semester's table with a first row spanning the whole
table - "I Year - Semester I", "I Year - Semester II" - over the same
columns. That row is the only thing telling the two tables apart, so it is
kept as the table's first row, inside its span: the table chunk, its every
reader, and the table beside it all see it where it was drawn. The header is
the row under it, when that row reads as a header; otherwise the guess is the
one made without a title.
"""

import os
import tempfile

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import compose, pdf


def run(*pages):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path)


def ruled(page):
    return [table for table in page.tables if table.source == pdf.SOURCE_RULED]


def titled(page, x, y, title, rows, widths):
    """A ruled table whose first row is `title`, merged across every column."""
    first = [title] + [""] * (len(widths) - 1)
    page.grid(
        x,
        y,
        widths,
        [first, *rows],
        merge_right={(0, column) for column in range(len(widths) - 1)},
    )
    return page


SEMESTER_I = [
    ["S.No", "Course Code", "Course Title", "Credits"],
    ["1", "23CS101", "Engineering Mathematics", "4"],
    ["2", "23CS102", "Programming in C", "3"],
    ["3", "23CS103", "Engineering Physics", "3"],
]
SEMESTER_II = [
    ["S.No", "Course Code", "Course Title", "Credits"],
    ["1", "23CS201", "Data Structures", "4"],
    ["2", "23CS202", "Digital Logic Design", "3"],
    ["3", "23CS203", "Engineering Chemistry", "3"],
]
WIDTHS = [40, 90, 200, 60]


def semesters():
    page = fx.Page().text(72, 60, "B.Tech CSE Course Structure (R23)")
    titled(page, 72, 100, "I Year - Semester I", SEMESTER_I, WIDTHS)
    titled(page, 72, 260, "I Year - Semester II", SEMESTER_II, WIDTHS)
    return page


class TitleRowTests(SimpleTestCase):
    def test_the_title_stays_the_first_row_of_its_table(self):
        (page,) = run(semesters())

        first, second = ruled(page)
        self.assertEqual(first.rows[0], ["I Year - Semester I", "", "", ""])
        self.assertEqual(second.rows[0], ["I Year - Semester II", "", "", ""])
        span = page.text[second.character_start : second.character_end]
        self.assertTrue(span.startswith("I Year - Semester II"))
        self.assertIn("Data Structures", span)

    def test_the_header_is_the_row_under_the_title(self):
        (page,) = run(semesters())

        for table in ruled(page):
            self.assertEqual(table.header, SEMESTER_I[0])
            self.assertEqual(table.rows[1], SEMESTER_I[0])

    def test_side_by_side_tables_keep_their_own_titles(self):
        page = fx.Page()
        rows_i = [["Course", "Credits"], ["Physics", "4"], ["Drawing", "3"]]
        rows_ii = [["Course", "Credits"], ["Chemistry", "4"], ["Workshop", "2"]]
        titled(page, 72, 100, "SEMESTER I", rows_i, [140, 60])
        titled(page, 330, 100, "SEMESTER II", rows_ii, [140, 60])

        (result,) = run(page)

        left, right = ruled(result)
        self.assertEqual(left.rows[0][0], "SEMESTER I")
        self.assertEqual(right.rows[0][0], "SEMESTER II")
        text = result.text
        self.assertLess(text.index("Drawing"), text.index("SEMESTER II"))
        self.assertEqual(text[right.character_start :].split(" |")[0], "SEMESTER II")

    def test_a_title_over_a_single_row_is_a_table_of_two_rows(self):
        page = fx.Page()
        titled(
            page,
            72,
            100,
            "CROSS-CUTTING COURSES",
            [["III-I", "Sociology", "NPTEL"]],
            [100, 150, 150],
        )

        (result,) = run(page)

        (table,) = ruled(result)
        self.assertEqual(
            table.rows,
            [["CROSS-CUTTING COURSES", "", ""], ["III-I", "Sociology", "NPTEL"]],
        )

    def test_a_short_label_over_one_column_is_not_a_title(self):
        """A first row whose only text sits in one narrow cell is read as the
        table's own first row: the header is guessed over the whole table."""
        page = fx.Page()
        page.grid(
            72,
            100,
            [80, 200, 80],
            [["Core", "", ""], *[row[1:] for row in SEMESTER_I]],
        )

        (result,) = run(page)

        (table,) = ruled(result)
        self.assertEqual(table.header, ["Core", "", ""])


class HeaderUnderTitleTests(SimpleTestCase):
    """The row under a title is taken for the header only when it reads as one."""

    def test_a_forms_first_line_is_not_its_header(self):
        page = fx.Page()
        titled(
            page,
            72,
            100,
            "STUDENT DETAILS",
            [
                ["Name", "K. Asha"],
                ["Date of Birth", "12/05/2006"],
                ["Gender", "Female"],
                ["Mobile", "9876543210"],
            ],
            [120, 160],
        )

        (result,) = run(page)

        (table,) = ruled(result)
        self.assertEqual(table.header, ["STUDENT DETAILS", ""])

    def test_a_row_holding_a_value_is_not_the_header(self):
        page = fx.Page()
        titled(
            page,
            72,
            100,
            "Fee details 2025-26",
            [
                ["Tuition Fee", "Rs. 45,000"],
                ["Examination Fee", "1,800"],
                ["Library Fee", "500"],
            ],
            [160, 120],
        )

        (result,) = run(page)

        (table,) = ruled(result)
        self.assertEqual(table.header, ["Fee details 2025-26", ""])


# -- the title of the next table, drawn touching -------------------------------


def touching_titled(group_label=False):
    """A course list and, under the same border, a titled list of streams.

    With `group_label`, a "Core" label merged down the course rows reaches
    into the title row too: that row is then part of the "Core" group.
    """
    page = fx.Page()
    left, right = 72, 372
    for y in (100, 120, 180, 200, 220):
        page.rule(left, y, right, y)
    for y in (140, 160):
        page.rule(172 if group_label else left, y, right, y)
    for x in (left, right):
        page.rule(x, 100, x, 220)
    page.rule(172, 100, 172, 180 if group_label else 160)
    page.rule(272, 100, 272, 160)
    page.rule(222, 180, 222, 220)
    first = "Group" if group_label else "Code"
    for y, cells in (
        (105, [first, "Course", "Credits"]),
        (125, ["Core" if group_label else "CS101", "Programming", "4"]),
        (145, ["" if group_label else "CS102", "Databases", "3"]),
    ):
        for x, cell in zip((76, 176, 276), cells, strict=True):
            if cell:
                page.text(x, y, cell)
    page.text(176 if group_label else 76, 165, "ELECTIVE STREAMS")
    for y, cells in ((185, ["Stream", "Elective"]), (205, ["PE-1", "Robotics"])):
        for x, cell in zip((76, 226), cells, strict=True):
            page.text(x, y, cell)
    return page


class TouchingTitleTests(SimpleTestCase):
    def test_a_title_between_two_touching_tables_starts_the_second(self):
        (result,) = run(touching_titled())

        courses, streams = ruled(result)
        self.assertEqual(courses.rows[-1], ["CS102", "Databases", "3"])
        self.assertEqual(streams.rows[0], ["ELECTIVE STREAMS", ""])
        self.assertEqual(
            streams.rows[1:], [["Stream", "Elective"], ["PE-1", "Robotics"]]
        )

    def test_a_row_a_merged_group_reaches_stays_in_the_group(self):
        (result,) = run(touching_titled(group_label=True))

        courses, streams = ruled(result)
        self.assertIn("ELECTIVE STREAMS", courses.rows[-1])
        self.assertEqual(streams.rows, [["Stream", "Elective"], ["PE-1", "Robotics"]])


# -- the writer ---------------------------------------------------------------


class TitleRowsWriterTests(SimpleTestCase):
    """compose.PageBuilder.add_table(title_rows=...)."""

    def header(self, rows, **options):
        builder = compose.PageBuilder()
        builder.add_table(rows, **options)
        return builder.tables[0].header

    def test_the_header_is_guessed_under_the_title(self):
        rows = [["Semester I", "", ""], *[row[1:] for row in SEMESTER_I]]

        self.assertEqual(self.header(rows, title_rows=1), SEMESTER_I[0][1:])
        # Without a title the first row is the guess, as it always was.
        self.assertEqual(self.header(rows), ["Semester I", "", ""])

    def test_nothing_under_the_title_reading_as_a_header_falls_back(self):
        rows = [["Marks", ""], ["Asha", "91"], ["Ravi", "78"]]

        self.assertEqual(self.header(rows, title_rows=1), ["Marks", ""])

    def test_a_header_given_is_the_row_under_the_title(self):
        rows = [["Pools", "", ""], ["Pool", "Code", "Credits"], ["Pool-I", "C1", "4"]]

        self.assertEqual(
            self.header(rows, header=True, title_rows=1), ["Pool", "Code", "Credits"]
        )
        self.assertEqual(self.header(rows, header=True), ["Pools", "", ""])

    def test_a_column_of_figures_needs_two_of_them(self):
        one = [["Lab", ""], ["Code", "Credits"], ["23ME4216", "1.5"]]
        two = [*one, ["23ME4217", "2"]]

        self.assertEqual(self.header(one, title_rows=1), ["Lab", ""])
        self.assertEqual(self.header(two, title_rows=1), ["Code", "Credits"])

    def test_values_are_told_from_labels(self):
        for value in (
            "Rs. 45,000",
            "(1,200)",
            "12.5%",
            "Name: Asha",
            "12 Mar 2025",
            "03/04/2019",
            "21A91A0501",
            "9876543210",
            "asha@example.com",
            "Apply various machining operations using lathe machines",
        ):
            with self.subTest(value=value):
                self.assertTrue(compose.looks_like_value(value))
        for label in (
            "Periods/Week L",
            "Course Code",
            "S. No.",
            "Credits",
            "2nd Year",
            "PO1",
            "Max. Marks",
            "TEXT BOOKS:",
            "",
        ):
            with self.subTest(label=label):
                self.assertFalse(compose.looks_like_value(label))
