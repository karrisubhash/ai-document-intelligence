"""Merged cells in a ruled PDF table: headers split over rows, labels merged down.

A header split over two or three rows ("Periods/Week" over L, T, P beside
"Course Code" merged down) is one header row. Nothing else is: the first rows
of a form - a photo merged down beside "Name of Candidate" - stay rows, as do
header rows with no text or with a value in them. A label merged down a group
of rows ("Pool-I") is written into every record of the group; a merged figure
is written once, and nothing is written into a row that only continues the
record above it, or from a title or header row.
"""

import os
import tempfile

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import pdf


def run(page):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, [page])
        (result,) = pdf.extract_page_results(path)
        return result


def table_of(page):
    (table,) = [t for t in run(page).tables if t.source == pdf.SOURCE_RULED]
    return table


def grid(widths, rows, **merges):
    return fx.Page().grid(72, 100, widths, rows, **merges)


class SplitHeaderTests(SimpleTestCase):
    def test_a_three_level_header_is_one_row(self):
        table = table_of(
            grid(
                [40, 90, 60, 60, 60, 60],
                [
                    ["S.No", "Course", "Scheme of Examination", "", "", ""],
                    ["", "", "Theory", "", "Practical", ""],
                    ["", "", "Int", "Ext", "Int", "Ext"],
                    ["1", "DBMS", "30", "70", "25", "50"],
                    ["2", "Networks", "30", "70", "25", "50"],
                ],
                merge_right={(0, 2), (0, 3), (0, 4), (1, 2), (1, 4)},
                merge_down={(0, 0), (1, 0), (0, 1), (1, 1)},
            )
        )

        scheme = "Scheme of Examination"
        self.assertEqual(
            table.header,
            [
                "S.No",
                "Course",
                f"{scheme} Theory Int",
                f"{scheme} Theory Ext",
                f"{scheme} Practical Int",
                f"{scheme} Practical Ext",
            ],
        )
        self.assertEqual(table.rows[1], ["1", "DBMS", "30", "70", "25", "50"])

    def test_a_forms_first_rows_beside_a_photo_stay_rows(self):
        """The photo is merged down three rows and the candidate's name
        across three columns, but "Father Name" starts in the second row
        under nothing merged across: these are records, not a header."""
        table = table_of(
            grid(
                [110, 90, 90, 90, 80],
                [
                    ["Name of Candidate", "ASHA RAO", "", "", "PHOTO"],
                    ["Father Name", "RAMESH RAO", "Date of Birth", "01-01-2000", ""],
                    ["Category", "OBC", "Gender", "Female", ""],
                    ["Address", "12 MG Road", "", "", ""],
                ],
                merge_right={(0, 1), (0, 2), (3, 1), (3, 2), (3, 3)},
                merge_down={(0, 4), (1, 4)},
            )
        )

        self.assertEqual(
            table.rows,
            [
                ["Name of Candidate", "ASHA RAO", "", "", "PHOTO"],
                ["Father Name", "RAMESH RAO", "Date of Birth", "01-01-2000", ""],
                ["Category", "OBC", "Gender", "Female", ""],
                ["Address", "12 MG Road", "", "", ""],
            ],
        )

    def test_a_header_holding_a_value_stays_two_rows(self):
        table = table_of(
            grid(
                [60, 120, 120],
                [
                    ["Q.No", "Max. Marks: 70", ""],
                    ["", "Part A", "Part B"],
                    ["1", "10", "20"],
                    ["2", "10", "20"],
                ],
                merge_right={(0, 1)},
                merge_down={(0, 0)},
            )
        )

        self.assertEqual(
            table.rows[:2], [["Q.No", "Max. Marks: 70", ""], ["", "Part A", "Part B"]]
        )

    def test_a_header_with_no_text_is_not_a_header(self):
        """Labels drawn as pictures leave the header rows blank: the first
        data row must not become the header in their place."""
        table = table_of(
            grid(
                [80, 50, 50, 50, 60],
                [
                    ["", "", "", "", ""],
                    ["", "", "", "", ""],
                    ["Pool-I", "3", "1", "0", "4"],
                    ["Pool-II", "3", "0", "2", "4"],
                ],
                merge_right={(0, 1), (0, 2)},
                merge_down={(0, 0), (0, 4)},
            )
        )

        self.assertEqual(table.header, [])
        self.assertEqual(table.rows[0], ["Pool-I", "3", "1", "0", "4"])

    def test_a_header_with_no_row_under_it_stays_two_rows(self):
        page = grid(
            [80, 40, 40, 40],
            [["Code", "Periods", "", ""], ["", "L", "T", "P"]],
            merge_right={(0, 1), (0, 2)},
            merge_down={(0, 0)},
        )

        self.assertEqual(
            table_of(page).rows, [["Code", "Periods", "", ""], ["", "L", "T", "P"]]
        )

    def test_a_header_of_four_levels_stays_as_drawn(self):
        table = table_of(
            grid(
                [80, 60, 60],
                [
                    ["Course", "Marks", ""],
                    ["", "Internal", ""],
                    ["", "Theory", ""],
                    ["", "Th", "Pr"],
                    ["DBMS", "30", "20"],
                ],
                merge_right={(0, 1), (1, 1), (2, 1)},
                merge_down={(0, 0), (1, 0), (2, 0)},
            )
        )

        self.assertEqual(table.rows[0], ["Course", "Marks", ""])
        self.assertEqual(len(table.rows), 5)

    def test_a_header_row_with_a_hole_in_it_stays_as_drawn(self):
        """No cell at all under "Code" - its left border is not drawn in the
        second row - so "Code" neither runs down the header nor is
        subdivided."""
        page = grid(
            [80, 60, 60, 60],
            [
                ["Code", "Periods", "", "Credits"],
                ["", "L", "T", ""],
                ["CS101", "3", "1", "4"],
                ["CS102", "2", "0", "3"],
            ],
            merge_right={(0, 1)},
            merge_down={(0, 3)},
        )
        left_border_of_row_two = (
            f"0.8 w 72 {fx.PAGE_HEIGHT - 120:.2f} m 72 {fx.PAGE_HEIGHT - 140:.2f} l S"
        )
        page.ops.remove(left_border_of_row_two)

        table = table_of(page)

        self.assertEqual(table.rows[0], ["Code", "Periods", "", "Credits"])
        self.assertEqual(table.rows[1], ["", "L", "T", ""])

    def test_a_cell_running_out_from_under_a_merged_one_is_not_a_header(self):
        """ "Total" starts under "Periods" but runs on under "Hours"."""
        table = table_of(
            grid(
                [80, 60, 60, 60],
                [
                    ["Code", "Periods", "", "Hours"],
                    ["", "L", "Total", ""],
                    ["CS101", "3", "4", ""],
                    ["CS102", "2", "3", ""],
                ],
                merge_right={(0, 1), (1, 2)},
                merge_down={(0, 0)},
            )
        )

        self.assertEqual(
            table.rows[:2], [["Code", "Periods", "", "Hours"], ["", "L", "Total", ""]]
        )

    def test_a_header_cell_running_into_the_body_is_not_a_header(self):
        table = table_of(
            grid(
                [80, 60, 60],
                [
                    ["Code", "Periods", ""],
                    ["", "L", "T"],
                    ["CS101", "3", ""],
                    ["CS102", "2", "1"],
                ],
                merge_right={(0, 1)},
                merge_down={(0, 0), (1, 2)},
            )
        )

        self.assertEqual(table.rows[:2], [["Code", "Periods", ""], ["", "L", "T"]])


class FillDownTests(SimpleTestCase):
    def test_a_continuation_row_of_figures_gets_no_label(self):
        """A lab's code and category are merged down beside its L, T, P
        header labels and the values under them; the row of values is the
        same course, not a second one."""
        table = table_of(
            grid(
                [80, 60, 30, 30, 30, 60],
                [
                    ["Code", "Category", "L", "T", "P", "Credits"],
                    ["23ME4216", "PC", "L", "T", "P", "1.5"],
                    ["", "", "-", "-", "3", ""],
                ],
                merge_down={(1, 0), (1, 1), (1, 5)},
            )
        )

        self.assertEqual(table.rows[2], ["", "", "-", "-", "3", ""])

    def test_a_merged_total_is_written_once(self):
        table = table_of(
            grid(
                [50, 250, 60],
                [
                    ["Q.No", "Question", "Marks"],
                    ["1", "Define a database.", "10"],
                    ["2", "What is a key?", ""],
                    ["3", "Define a schema.", ""],
                ],
                merge_down={(1, 2), (2, 2)},
            )
        )

        self.assertEqual([row[2] for row in table.rows[1:]], ["10", "", ""])

    def test_a_merged_note_right_of_the_records_is_written_once(self):
        table = table_of(
            grid(
                [50, 120, 160],
                [
                    ["S.No", "Item", "Remarks"],
                    ["1", "Chair", "Deliver by Friday"],
                    ["2", "Desk", ""],
                    ["3", "Lamp", ""],
                ],
                merge_down={(1, 2), (2, 2)},
            )
        )

        self.assertEqual(
            [row[2] for row in table.rows[1:]], ["Deliver by Friday", "", ""]
        )

    def test_a_cell_merged_down_from_the_first_row_is_written_once(self):
        """A photo beside the first lines of a form is not a group label."""
        table = table_of(
            grid(
                [80, 120, 160],
                [
                    ["PHOTO", "Name", "K. Asha"],
                    ["", "Roll No", "21A91A0501"],
                    ["", "Branch", "Computer Science"],
                ],
                merge_down={(0, 0), (1, 0)},
            )
        )

        self.assertEqual([row[0] for row in table.rows], ["PHOTO", "", ""])
