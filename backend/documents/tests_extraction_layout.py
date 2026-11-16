"""Reading order and tables from word positions (extractors/geometry.py).

The cases here are layouts that look like something else to a rule written
for one document: a label beside several lines of text that is NOT a margin
note, a total row padded with dots that is NOT a table of contents, a blank
to fill in that is NOT a leader. Each was read correctly before a rule
broke it, and each is here so that no rule can break it again.
"""

import os
import tempfile

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import geometry, pdf


def run(*pages):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path)


def lines_of(result):
    return [line for line in result.text.splitlines() if line.strip()]


# -- a label beside the lines it labels ---------------------------------------

UNITS = [
    (
        "UNIT I",
        [
            "Introduction to operating systems: definition, functions and",
            "types of operating systems, system calls, structure of an",
            "operating system, and virtual machines.",
        ],
    ),
    (
        "UNIT II",
        [
            "Process management: process concept, scheduling criteria and",
            "algorithms, threads, inter-process communication and",
            "synchronisation, the critical-section problem.",
        ],
    ),
    (
        "UNIT III",
        [
            "Memory management: swapping, contiguous allocation, paging,",
            "segmentation, virtual memory, demand paging and page",
            "replacement algorithms with thrashing.",
        ],
    ),
    (
        "UNIT IV",
        [
            "File systems: file concept, access methods, directory",
            "structure, allocation methods, free space management and",
            "protection, with implementation issues.",
        ],
    ),
]


def syllabus_units():
    page = fx.Page().text(72, 60, "Course Content", size=14, font="F2")
    y = 100
    for label, content in UNITS:
        page.text(72, y, label)
        for offset, line in enumerate(content):
            page.text(140, y + 14 * offset, line)
        y += 14 * len(content) + 10
    return page


class LabelBesideTextTests(SimpleTestCase):
    """The label stays with the lines beside it - in one table row, or on its line."""

    def labelled(self, result, label):
        """Every line or table row of the page that holds `label`."""
        return [line for line in lines_of(result) if label in line]

    def test_unit_labels_stay_with_their_topics(self):
        (result,) = run(syllabus_units())

        rows = [row for table in result.tables for row in table.rows]
        by_label = {
            row[0]: " ".join(row[1:]) for row in rows if row[0].startswith("UNIT")
        }
        self.assertEqual(sorted(by_label), ["UNIT I", "UNIT II", "UNIT III", "UNIT IV"])
        self.assertIn("virtual machines", by_label["UNIT I"])
        self.assertIn("critical-section problem", by_label["UNIT II"])
        self.assertIn("thrashing", by_label["UNIT III"])
        # The last row's own continuation lines are trimmed off the band by
        # design (text under a table is not a row); its first line is not.
        self.assertIn("File systems", by_label["UNIT IV"])

    def test_every_label_is_followed_by_its_own_text_not_by_the_next_label(self):
        (result,) = run(syllabus_units())
        text = result.text

        for (label, content), (next_label, _) in zip(UNITS, UNITS[1:], strict=False):
            self.assertLess(text.index(label), text.index(content[0].split(":")[0]))
            self.assertLess(text.index(content[-1].split()[-1]), text.index(next_label))

    def test_course_outcome_labels_with_a_space_stay_with_their_outcomes(self):
        page = fx.Page()
        outcomes = [
            "Apply the principles of thermodynamics to analyse energy",
            "Analyse cycles and calculate efficiency of heat engines",
            "Evaluate the performance of refrigeration and air",
            "Design a simple power plant layout for a given load",
        ]
        y = 100
        for number, outcome in enumerate(outcomes, start=1):
            page.text(72, y, f"CO {number}")
            page.text(140, y, outcome)
            page.text(140, y + 14, "and justify the choices made in the design.")
            page.text(140, y + 28, "Students record the results in a report.")
            y += 54

        (result,) = run(page)

        for number, outcome in enumerate(outcomes, start=1):
            together = [line for line in lines_of(result) if f"CO {number}" in line]
            self.assertTrue(together, f"CO {number} lost")
            self.assertIn(outcome.split()[0], together[0])

    def test_dates_stay_on_the_line_of_their_job_title(self):
        page = fx.Page().text(72, 60, "Experience", size=14, font="F2")
        jobs = [
            ("Software Engineer, Infosys Ltd, Bangalore", "Jun 2021 - Present"),
            ("Intern, Wipro Technologies, Chennai", "Jul 2019 - May 2021"),
            ("Trainee, TCS, Hyderabad", "Jan 2019 - Jun 2019"),
        ]
        y = 100
        for title, dates in jobs:
            page.text(72, y, title)
            page.text(420, y, dates)
            page.text(90, y + 14, "- Wrote Grafana dashboards for the team")
            page.text(90, y + 28, "- Fixed flaky tests in the CI pipeline")
            y += 56

        (result,) = run(page)

        for title, dates in jobs:
            line = next(line for line in lines_of(result) if title in line)
            self.assertIn(dates, line)

    def test_marks_stay_on_the_line_of_their_question(self):
        page = fx.Page().text(72, 60, "Part B", size=14, font="F2")
        questions = [
            ("1. Explain the architecture of the 8086 microprocessor with", "[10M]"),
            ("2. Compare memory mapped input and output with isolated", "[10M]"),
            ("3. Describe the interrupt structure of the 8051 and give", "[12M]"),
            ("4. Write an assembly program to sort an array of numbers", "[8M]"),
        ]
        y = 100
        for question, marks in questions:
            page.text(72, y, question)
            page.text(500, y, marks)
            page.text(
                90, y + 14, "a neat diagram wherever it is necessary for the answer."
            )
            page.text(
                90, y + 28, "State every assumption that you make in the process."
            )
            y += 54

        (result,) = run(page)

        for question, marks in questions:
            line = next(line for line in lines_of(result) if question[:20] in line)
            self.assertIn(marks, line)

    def test_a_form_with_blank_values_keeps_its_table_and_its_blanks(self):
        page = fx.Page().text(72, 60, "Application", size=14, font="F2")
        fields = [
            ("Name", "Asha Rao"),
            ("Roll No", "21A91A0501"),
            ("Section", ""),
            ("Branch", "CSE"),
            ("Blood Group", ""),
            ("Hostel", ""),
            ("Email", "asha@example.com"),
        ]
        for index, (label, value) in enumerate(fields):
            page.text(72, 100 + 20 * index, label)
            if value:
                page.text(250, 100 + 20 * index, value)

        (result,) = run(page)

        rows = [row for table in result.tables for row in table.rows]
        self.assertIn(["Section", ""], rows)
        self.assertIn(["Hostel", ""], rows)
        self.assertIn(["Name", "Asha Rao"], rows)


# -- contents, leaders and blanks --------------------------------------------


class ContentsTests(SimpleTestCase):
    def test_a_page_number_directly_after_a_leader_is_a_contents_line(self):
        for text in (
            "1.1 eSim . . . . . . . . . . . . . 4",
            "Introduction ............................ 12",
            "Preface . . . . . . . iv",
            "3.1.1 Approach . . . . . . . . . . . . 8",
        ):
            with self.subTest(text=text):
                self.assertTrue(geometry._is_contents_line(text))

    def test_a_value_after_a_leader_is_not_a_page_number(self):
        for text in (
            "Total payable .......... 20000.00",
            "Sub Total ........ 6230.00",
            "SGPA ........ 8.25",
            "Year of study .......... III",
            "Branch .......... Civil",
            "Date of Birth .......... 12.05.2004",
            "Registration fee .......... Rs. 500",
            "Total .......... 94000 89000",
            "Amount ........ 20000",
        ):
            with self.subTest(text=text):
                self.assertFalse(geometry._is_contents_line(text))

    def test_a_line_with_two_leaders_is_not_a_contents_line(self):
        self.assertFalse(
            geometry._is_contents_line("Name ......... Roll .......... 12")
        )

    def test_one_row_with_a_leader_does_not_make_a_band_a_contents_page(self):
        rows = [["Room rent", "36000"]] * 6 + [["Total payable ..........", "500"]]

        self.assertFalse(geometry._is_contents_band(rows))

    def test_most_rows_with_leaders_do(self):
        entry = ["Chapter . . . . . . . . . .", "7"]
        self.assertTrue(geometry._is_contents_band([entry, entry, ["Preface", "iii"]]))
        self.assertFalse(geometry._is_contents_band([entry]))

    def test_contents_lines_are_lines_with_their_dots_intact(self):
        entries = [
            ("1.1", "eSim", "4"),
            ("1.1.1", "NgSpice . . . . . . . . . . . . . . .", "5"),
            ("1.1.2", "Makerchip . . . . . . . . . . . . . .", "5"),
            ("1.1.3", "KiCad . . . . . . . . . . . . . . . .", "5"),
            ("1.1.4", "GHDL . . . . . . . . . . . . . . . . .", "6"),
        ]
        page = fx.Page()
        for index, (number, title, at) in enumerate(entries):
            y = 100 + 14 * index
            page.text(72, y, number)
            page.text(110, y, title)
            page.text(450, y, at)

        (result,) = run(page)

        self.assertEqual(result.tables, [])
        self.assertIn("1.1.2 Makerchip . . . . . . . . . . . . . . 5", result.text)

    def test_a_fee_table_with_a_dotted_total_is_still_a_table_with_its_blanks(self):
        page = fx.Page().text(72, 60, "Hostel fee structure", size=14, font="F2")
        heads = ["Particulars", "Boys", "Girls", "Day Scholars"]
        xs = [72, 220, 300, 380]
        rows = [
            ("Room rent", "36000", "38000", ""),
            ("Mess advance", "24000", "24000", ""),
            ("Bus fee", "", "", "18000"),
            ("Caution deposit", "5000", "5000", "2000"),
            ("Total payable ..........", "", "", "20000.00"),
        ]
        for x, head in zip(xs, heads, strict=True):
            page.text(x, 100, head)
        for index, row in enumerate(rows):
            for column, cell in enumerate(row):
                if not cell:
                    continue
                y = 120 + 16 * index
                x = xs[column] if column == 0 else xs[column] + 40 - 5.6 * len(cell)
                page.text(x, y, cell)

        (result,) = run(page)

        table_rows = [row for table in result.tables for row in table.rows]
        self.assertIn(["Bus fee", "", "", "18000"], table_rows)

    def test_a_blank_to_fill_in_is_not_rewritten(self):
        page = fx.Page().text(
            72,
            100,
            "1. The capital of India is ................ and of Telangana is ........",
        )
        page.text(72, 114, "2. Weight of the sample . . . . .5 kg after drying.")
        page.text(72, 128, "3. The string ...... holds six dots.")

        (result,) = run(page)

        self.assertIn("is ................ and", result.text)
        self.assertIn(". . . . .5 kg", result.text)
        self.assertIn("The string ...... holds", result.text)

    def test_a_two_column_page_with_a_blank_in_one_line_is_still_two_columns(self):
        left = [
            "The annual sports meet was held on the",
            "main ground with more than six hundred",
            "students taking part in track events and",
            "the date given here: ................ for",
            "the finals which were held the next week.",
            "The winners received their medals from",
        ]
        right = [
            "The library has added two thousand new",
            "titles to its engineering collection and",
            "extended its opening hours on weekdays",
            "and the average daily footfall rose to 3.5",
            "visits per student over the whole semester.",
            "The reading room now stays open until ten.",
        ]
        page = fx.Page()
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            page.text(72, 100 + 14 * index, a)
            page.text(320, 100 + 14 * index, b)

        (result,) = run(page)
        text = result.text

        self.assertLess(text.index(left[0]), text.index(left[-1]))
        self.assertLess(text.index(left[-1]), text.index(right[0]))
        self.assertLess(text.index(right[0]), text.index(right[-1]))
