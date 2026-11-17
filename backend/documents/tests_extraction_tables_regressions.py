"""Tables the rework once read worse than the code before it did.

Found by the second review of the ruled-table reading, each one a document
the code that was live read correctly: a faculty list ruled only around its
columns (one row of six names, not six rows), a header bar whose cell borders
are the bar's own colour (one header cell, not three), a measure merged down
a group of rows ("9 Hrs" written into each, so a sum of hours counts it per
topic), a question paper in which a few questions have sub-parts (distrusted
as a whole), and a contents page, which the page reader judged again from each
of its lines.
"""

import os
import tempfile
from unittest import mock

import pdfplumber
from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import geometry, pdf, rulings

BLACK = (0, 0, 0)


def run(page):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, [page])
        (result,) = pdf.extract_page_results(path)
        return result


def ruled(result):
    return [t for t in result.tables if t.source == pdf.SOURCE_RULED]


# -- lists ruled only around their columns --------------------------------------

FACULTY = [
    ("Dr. K. Srinivasa Rao", "Professor & HOD", "Ph.D", "Computer Science"),
    ("Dr. P. Lakshmi", "Professor", "Ph.D", "Electronics"),
    ("Mr. A. Ravi Kumar", "Associate Professor", "M.Tech", "Mechanical"),
    ("Mrs. S. Anitha", "Assistant Professor", "M.Tech", "Civil"),
]
FACULTY_HEADER = ["Name", "Designation", "Qualification", "Department"]


def faculty_page():
    """A header ruled off and a box round the rest: no rule between people."""
    page = fx.Page()
    edges = [60, 190, 310, 380, 500]
    bottom = 120 + len(FACULTY) * 16 + 6
    for y in (100, 120, bottom):
        page.rule(edges[0], y, edges[-1], y)
    for x in edges:
        page.rule(x, 100, x, bottom)
    for x, label in zip(edges, FACULTY_HEADER, strict=False):
        page.text(x + 4, 105, label)
    for r, record in enumerate(FACULTY):
        for x, cell in zip(edges, record, strict=False):
            page.text(x + 4, 124 + r * 16, cell, size=9)
    return page


class StackedListTests(SimpleTestCase):
    def test_a_list_ruled_only_around_its_columns_is_read_a_row_per_person(self):
        result = run(faculty_page())

        (table,) = result.tables
        self.assertEqual(table.rows, [FACULTY_HEADER, *map(list, FACULTY)])
        self.assertEqual(result.metadata.get("ruled_tables_distrusted"), 1)

    def test_names_stacked_in_every_column_are_a_band_of_rows(self):
        rows = [
            FACULTY_HEADER,
            [
                "Dr. K. Rao\nDr. P. Lakshmi\nMr. A. Kumar",
                "Professor\nProfessor\nAssociate Professor",
                "Ph.D\nPh.D\nM.Tech",
                "Computer Science\nElectronics\nMechanical",
            ],
        ]

        self.assertFalse(pdf._rows_are_whole(rows))

    def test_a_column_of_lower_case_items_is_still_a_stack(self):
        rows = [
            ["Name", "Designation", "Email", "Result"],
            [
                "K. Rao\nP. Lakshmi\nA. Kumar\nS. Anitha",
                "Professor\nProfessor\nAssociate Professor\nLecturer",
                "krao@college.edu\nplakshmi@college.edu\nakumar@college.edu\n"
                "sanitha@college.edu",
                "pass\nfail\nabsent\npass",
            ],
        ]

        self.assertFalse(pdf._rows_are_whole(rows))

    def test_a_designation_that_wraps_in_every_band_is_still_a_band(self):
        rows = [
            ["Name", "Designation", "Qualification", "Department"],
            [
                "K. Rao\nP. Lakshmi\nA. Kumar\nS. Anitha",
                "Professor and Head of\nthe Department\nProfessor\nAssociate\n"
                "Professor\nLecturer",
                "Ph.D\nPh.D\nM.Tech\nM.Tech",
                "CSE\nECE\nMECH\nCIVIL",
            ],
        ]

        self.assertFalse(pdf._rows_are_whole(rows))

    def test_one_record_with_a_list_in_each_cell_is_a_row(self):
        """Unit | Topics | Outcomes with a single unit: three cells are one
        record, not a band - the code before this one trusted it too."""
        rows = [
            ["Unit", "Topics", "Outcomes"],
            [
                "II",
                "SQL basics\nJoins\nSubqueries\nViews",
                "Write queries\nJoin tables\nNest queries\nDefine views",
            ],
        ]

        self.assertTrue(pdf._rows_are_whole(rows))

    def test_columns_about_as_long_as_one_another_are_stacks_but_unequal_ones_wait(
        self,
    ):
        names = "Asha Rao\nRavi Kumar\nSita Devi\nMohan Das"
        rolls = "21A501\n21A502\n21A503\n21A504"
        header = ["Name", "Branch", "Roll", "Section"]
        rows = [header, [names, "CSE\nECE\nMECH\nCIVIL", rolls, "A\nA\nB\nB"]]
        lopsided = [
            header,
            [names, "CSE\nECE\nMECH\nCIVIL\nEEE\nIT\nAI\nDS", rolls, "A\nA\nB\nB"],
        ]

        self.assertFalse(pdf._rows_are_whole(rows))
        self.assertTrue(pdf._rows_are_whole(lopsided))

    def test_wrapped_sentences_are_not_stacks_of_items(self):
        rows = [
            ["Term", "Meaning", "Example"],
            [
                "Cache",
                "A small fast memory\nthat keeps copies of\nfrequently used data.",
                "Reading the same\nfile twice\nreturns faster.",
            ],
        ]

        self.assertTrue(pdf._rows_are_whole(rows))

    def test_item_stacks_start_every_line_with_a_capital_or_a_digit(self):
        self.assertTrue(pdf._is_item_stack(["Asha Rao", "Ravi Kumar", "21A501"]))
        self.assertTrue(pdf._is_item_stack(["(a) Define", "(b) Explain", "(c) List"]))
        self.assertTrue(pdf._is_item_stack(["a@x.edu", "b@y.edu", "pass", "fail"]))
        self.assertFalse(pdf._is_item_stack(["Gives rights", "to a user", "on data"]))
        self.assertFalse(
            pdf._is_item_stack(
                [
                    "Explain the working of",
                    "a database system with",
                    "a neat sketch",
                    "Marks ten",
                ]
            )
        )
        self.assertFalse(pdf._is_item_stack(["only one line"]))


class QuestionPaperTests(SimpleTestCase):
    def test_a_few_questions_with_parts_among_single_ones_are_a_table(self):
        header = ["Q.No", "Question", "Marks", "CO"]
        with_parts = [
            "1",
            "a) Define normalization.\nb) Explain 2NF.\nc) Explain BCNF.",
            "2\n4\n4",
            "CO1\nCO1\nCO2",
        ]

        def single(number):
            return [str(number), "Explain the architecture of a DBMS.", "10", "CO1"]

        rows = [header, with_parts, single(2), single(3), single(4)]
        rows += [with_parts, single(6), single(7), with_parts, single(9), single(10)]

        # Three rows of ten: a table with a few banded rows is not a band.
        self.assertTrue(pdf._rows_are_whole(rows))

    def test_a_table_most_of_whose_rows_have_parts_is_still_a_band(self):
        header = ["Q.No", "Question", "Marks"]
        parts = ["1", "a) One.\nb) Two.\nc) Three.", "2\n4\n4"]
        whole = ["2", "Explain normalization.", "10"]
        rows = [header, parts, whole, parts, whole, parts]

        self.assertFalse(pdf._rows_are_whole(rows))

    def test_a_report_ruled_every_third_month_is_a_band(self):
        """Three bands of three months in a body of eight rows: more than a
        question paper's few questions in parts, and not a table."""
        header = ["Month", "Pax", "RPK", "ASK"]
        band = [
            "Apr-2015\nMay-2015\nJun-2015",
            "653,278\n686,908\n667,181",
            "2,958,643\n3,201,403\n3,198,199",
            "3,951,483\n4,118,309\n3,938,037",
        ]
        total = ["FY15 1st Q TTL", "2,007,367", "9,358,245", "12,008,829"]
        rows = [header, band, total, band, total, band, total, total, total]

        self.assertFalse(pdf._rows_are_whole(rows))

    def test_a_table_of_nothing_but_parts_is_still_a_band(self):
        header = ["Q.No", "Question", "Marks"]
        rows = [header] + [
            [str(n), "a) One.\nb) Two.\nc) Three.", "2\n4\n4"] for n in range(1, 5)
        ]

        self.assertFalse(pdf._rows_are_whole(rows))


# -- borders drawn as thin filled rectangles ------------------------------------

COLUMNS = [72, 162, 292, 382]
ROWS = [
    ["S.No", "Particulars", "Amount"],
    ["1", "Tuition Fee", "45,000"],
    ["2", "Hostel Fee", "30,000"],
]


def printed_table(border, strokes=True):
    """A table as a browser prints it: a dark header bar and, in it, a border
    between cells drawn as a thin rectangle in the bar's own colour."""
    page = fx.Page().fill(COLUMNS[0], 100, COLUMNS[-1] - COLUMNS[0], 20, BLACK)
    for x in COLUMNS:
        page.fill(x - 0.5, 100, 1, 20, border)
    for r in range(len(ROWS) + 1):
        page.rule(COLUMNS[0], 100 + r * 20, COLUMNS[-1], 100 + r * 20)
    for r in range(1, len(ROWS)):
        for x in COLUMNS:
            page.rule(x, 100 + r * 20, x, 100 + (r + 1) * 20)
    for r, row in enumerate(ROWS):
        for x, cell in zip(COLUMNS, row, strict=False):
            page.text(x + 4, 100 + r * 20 + 5, cell)
    return page


class ThinBorderTests(SimpleTestCase):
    def test_borders_in_the_colour_of_the_bar_still_divide_its_cells(self):
        (table,) = ruled(run(printed_table(BLACK)))

        self.assertEqual(table.rows, ROWS)
        self.assertEqual(table.header, ROWS[0])

    def test_a_thin_rectangle_is_a_line_and_a_cell_sized_one_is_not(self):
        page = (
            fx.Page()
            .fill(72, 100, 200, 20, BLACK)
            .fill(100, 100, 1, 20, BLACK)
            .fill(120, 100, 90, 20, BLACK)
            .fill(220, 105, 2, 2, BLACK)
        )
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "fixture.pdf")
            fx.write(path, [page])
            with pdfplumber.open(path) as document:
                plumbed = document.pages[0]
                hidden = rulings.invisible(plumbed)
                bar, thin, cell, speck = sorted(
                    plumbed.rects, key=lambda rect: rect["x0"]
                )

        self.assertNotIn(id(bar), hidden)
        self.assertNotIn(id(thin), hidden)
        self.assertIn(id(cell), hidden)
        # Thin one way is not enough: a speck is not a line.
        self.assertIn(id(speck), hidden)


# -- a merged measure ------------------------------------------------------------


def merged_groups(label, measure):
    """Unit and Hours merged down three topics; Aid has its own cell in each."""
    page = fx.Page()
    edges = [72, 152, 252, 312, 392]
    for y in (100, 120, 180):
        page.rule(edges[0], y, edges[-1], y)
    for y in (140, 160):
        page.rule(edges[1], y, edges[2], y)
        page.rule(edges[3], y, edges[4], y)
    for x in edges:
        page.rule(x, 100, x, 180)
    for x, head in zip(edges, ("Unit", "Topic", "Hours", "Aid"), strict=False):
        page.text(x + 4, 105, head)
    page.text(76, 145, label)
    page.text(256, 145, measure)
    for y, topic, aid in (
        (125, "Data models", "Chalk"),
        (145, "Architecture", "PPT"),
        (165, "Schemas", "Demo"),
    ):
        page.text(156, y, topic)
        page.text(316, y, aid)
    return page


class MergedMeasureTests(SimpleTestCase):
    def body(self, label, measure):
        (table,) = ruled(run(merged_groups(label, measure)))
        return table.rows[1:]

    def test_a_label_merged_down_is_written_into_every_topic(self):
        for label in ("UNIT I", "1st Year", "Core", "CO1", "Pool-I (III Year)"):
            with self.subTest(label=label):
                rows = self.body(label, "9 Hrs")
                self.assertEqual([row[0] for row in rows], [label] * 3)

    def test_a_measure_merged_down_stays_once_where_it_is_printed(self):
        for measure in (
            "9 Hrs",
            "10M",
            "45 min",
            "5 x 2 = 10M",
            "Rs. 500",
            "2 Credits",
        ):
            with self.subTest(measure=measure):
                rows = self.body("UNIT I", measure)
                self.assertEqual([row[2] for row in rows], [measure, "", ""])

    def test_labels_and_measures_are_told_apart(self):
        for measure in (
            "9 Hrs",
            "10M",
            "45 min",
            "5 x 2 = 10M",
            "Rs. 500",
            "(2 Hrs)",
            "3.5 Credits",
            "Rs. 5,000/-",
            "INR 5,000/-",
            "Rs.5000/-",
            "10 Marks each",
            "2 Hours 30 Min",
            "3 credits each",
            "Max. 100",
            "Total: 30",
            "40 to 60",
            "12 Periods/Week",
            "3-0-0 (L-T-P)",
            "2 1/2 Hrs",
            "30 Marks (Internal)",
            "3 Hours Theory",
        ):
            with self.subTest(measure=measure):
                self.assertFalse(rulings._is_label(measure))
        for label in (
            "UNIT I",
            "1st Year",
            "2nd Semester",
            "CO1",
            "Semester 3",
            "PO 2",
            "1st",
            "2nd",
            "3rd",
            "4th",
            "1 Year",
            "3 Sem",
            "8 Std",
            "10 Class",
            "1 PUC",
            "3A",
            "10A",
            "9B",
            "Module 3",
            "Total",
        ):
            with self.subTest(label=label):
                self.assertTrue(rulings._is_label(label))


# -- a contents page --------------------------------------------------------------


def contents_words(count):
    """`count` contents entries: a number, a title, a leader and a page."""
    words = []
    for index in range(count):
        top = 30 + index * 9.6
        bottom = top + 8
        number = f"{index // 10 + 1}.{index % 10 + 1}"
        words.append(geometry.Word(number, 40, 40 + 4 * len(number), top, bottom))
        x = 70
        for title in ("Section", "title", f"number{index}"):
            width = 4.4 * len(title)
            words.append(geometry.Word(title, x, x + width, top, bottom))
            x += width + 2.2
        words.append(geometry.Word("." * 60, x + 2, 520, top, bottom))
        page = str(index + 3)
        words.append(geometry.Word(page, 540, 540 + 4.4 * len(page), top, bottom))
    return words


class ContentsPageTests(SimpleTestCase):
    def test_a_contents_page_is_text_read_in_one_pass(self):
        words = contents_words(120)

        with mock.patch.object(
            geometry, "_grow_band", wraps=geometry._grow_band
        ) as grow:
            blocks = geometry.layout_page(words)

        self.assertEqual([block.kind for block in blocks], ["text"])
        self.assertEqual(len(blocks[0].text.split("\n")), 120)
        # Judged again from every line it would be 120 bands, each a little
        # shorter than the one before: minutes on a long page.
        self.assertLessEqual(grow.call_count, 3)

    def test_a_table_under_the_entries_is_still_found(self):
        words = contents_words(6)
        top = 30 + 6 * 9.6 + 20
        rows = [
            ["Name", "Marks"],
            ["Asha Rao", "91"],
            ["Ravi Kumar", "78"],
            ["Sita Devi", "84"],
        ]
        for r, (name, marks) in enumerate(rows):
            y = top + r * 12
            words.append(geometry.Word(name, 70, 70 + 4.4 * len(name), y, y + 8))
            words.append(geometry.Word(marks, 300, 300 + 4.4 * len(marks), y, y + 8))

        blocks = geometry.layout_page(words)

        self.assertEqual([block.kind for block in blocks], ["text", "table"])
        self.assertEqual(blocks[1].rows[1], ["Asha Rao", "91"])

    def test_only_the_entries_at_the_top_of_a_band_are_taken_as_text(self):
        """A table starting right under the entries is found: the lines after
        the last entry are judged on their own."""
        words = contents_words(6)
        top = 30 + 6 * 9.6
        rows = [
            ["Name", "Marks"],
            ["Asha Rao", "91"],
            ["Ravi Kumar", "78"],
            ["Sita Devi", "84"],
        ]
        for r, (name, marks) in enumerate(rows):
            y = top + r * 9.6
            words.append(geometry.Word(name, 70, 70 + 4.4 * len(name), y, y + 8))
            words.append(geometry.Word(marks, 300, 300 + 4.4 * len(marks), y, y + 8))

        blocks = geometry.layout_page(words)

        self.assertEqual([block.kind for block in blocks], ["text", "table"])
        self.assertEqual(blocks[0].text.count("\n"), 5)
        self.assertEqual(blocks[1].rows[1], ["Asha Rao", "91"])

    def test_a_block_placed_among_the_entries_lands_between_their_lines(self):
        words = contents_words(6)
        extra = geometry.Block(
            kind="text", top=30 + 3 * 9.6 - 1, bottom=30 + 3 * 9.6 - 1, text="PLACED"
        )

        blocks = geometry.layout_page(words, extra_blocks=[extra])

        self.assertEqual([block.text.count("\n") for block in blocks], [2, 0, 2])
        self.assertEqual(blocks[1].text, "PLACED")
