"""Spreadsheets and delimited files, read as tables.

Fixtures are real workbooks written with openpyxl, so each case states the
cells and the expected reading. Several of them are about the gap between how
Excel STORES a value and how a reader SEES it - a date is a number, 5% is
0.05, 0.1 + 0.2 is 0.30000000000000004 - because a table that prints the
stored form is a table nobody can search for what they read.
"""

import datetime as dt
import os
import tempfile
import time
import zipfile
from unittest import mock

import openpyxl
from django.test import SimpleTestCase

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import spreadsheet


def workbook_pages(build):
    """Extract the workbook `build(workbook)` fills in."""
    book = openpyxl.Workbook()
    build(book)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.xlsx")
        book.save(path)
        return spreadsheet.extract_xlsx(path)


def fees(book):
    sheet = book.active
    sheet.title = "Fees"
    sheet.append(["Programme", "Tuition", "Exam Fee"])
    sheet.append(["B.Tech CSE", 85000, 2500])
    sheet.append(["B.Tech ECE", None, 2500])
    sheet.append(["M.Tech VLSI", 95000.0, 3000])


class WorkbookTests(SimpleTestCase):
    def test_a_sheet_is_a_named_page_with_its_table_as_cells(self):
        (page,), metadata = workbook_pages(fees)

        self.assertTrue(page.text.startswith("Sheet: Fees\n\n"))
        self.assertEqual(
            page.tables[0].rows,
            [
                ["Programme", "Tuition", "Exam Fee"],
                ["B.Tech CSE", "85000", "2500"],
                ["B.Tech ECE", "", "2500"],
                ["M.Tech VLSI", "95000", "3000"],
            ],
        )
        self.assertEqual(page.tables[0].header, ["Programme", "Tuition", "Exam Fee"])
        self.assertEqual(page.tables[0].source, "openpyxl:sheet")
        self.assertEqual(page.metadata["sheet"], "Fees")
        self.assertEqual(metadata["sheets"], 1)

    def test_a_table_of_text_with_clear_column_names_still_gets_its_header(self):
        def build(book):
            sheet = book.active
            sheet.append(["Code", "Course", "Faculty"])
            sheet.append(["CS101", "Programming", "Asha Rao"])
            sheet.append(["CS102", "Data Structures", "Ravi Kumar"])

        (page,), _ = workbook_pages(build)

        self.assertEqual(page.tables[0].header, ["Code", "Course", "Faculty"])

    def test_a_two_column_list_does_not_invent_a_header(self):
        def build(book):
            sheet = book.active
            sheet.append(["Name", "Asha Rao"])
            sheet.append(["Department", "Computer Science"])
            sheet.append(["Roll No", "21CS045"])

        (page,), _ = workbook_pages(build)

        self.assertEqual(page.tables[0].header, [])

    def test_dates_and_times_are_written_as_a_reader_reads_them(self):
        def build(book):
            sheet = book.active
            sheet.append(["Event", "When"])
            sheet.append(["Convocation", dt.datetime(2024, 5, 3)])
            sheet.append(["Viva", dt.datetime(2024, 5, 3, 14, 30)])
            sheet.append(["Closing", dt.date(2024, 6, 1)])

        (page,), _ = workbook_pages(build)

        self.assertEqual(
            [row[1] for row in page.tables[0].rows[1:]],
            ["2024-05-03", "2024-05-03 14:30", "2024-06-01"],
        )

    def test_percentages_follow_the_cell_format(self):
        def build(book):
            sheet = book.active
            sheet.append(["Item", "Rate"])
            sheet.append(["Late fee", 0.0525])
            sheet.append(["Discount", 0.5])
            sheet["B2"].number_format = "0.00%"
            sheet["B3"].number_format = "0%"

        (page,), _ = workbook_pages(build)

        self.assertEqual([row[1] for row in page.tables[0].rows[1:]], ["5.25%", "50%"])

    def test_floating_point_noise_is_not_written_down(self):
        def build(book):
            sheet = book.active
            sheet.append(["Label", "Value"])
            sheet.append(["Sum", 0.1 + 0.2])
            sheet.append(["Whole", 85000.0])
            sheet.append(["Tiny", 1e-7])
            sheet.append(["Flag", True])

        (page,), _ = workbook_pages(build)

        self.assertEqual(
            [row[1] for row in page.tables[0].rows[1:]],
            ["0.3", "85000", "0.0000001", "TRUE"],
        )

    def test_a_formula_with_no_stored_result_reads_blank_not_as_its_text(self):
        """Written by a library that never calculated it, there is no value
        to read. Printing "=A2+1" would put a formula where a number belongs."""

        def build(book):
            sheet = book.active
            sheet.append(["A", "B"])
            sheet.append([1, "=A2+1"])
            sheet.append([2, 5])

        (page,), _ = workbook_pages(build)

        self.assertEqual(page.tables[0].rows[1], ["1", ""])

    def test_hidden_sheets_are_skipped_and_counted(self):
        def build(book):
            fees(book)
            secret = book.create_sheet("Scratch")
            secret.append(["x", "y"])
            secret.append([1, 2])
            secret.sheet_state = "hidden"

        pages, metadata = workbook_pages(build)

        self.assertEqual([p.metadata["sheet"] for p in pages], ["Fees"])
        self.assertEqual(metadata["sheets_hidden"], 1)

    def test_each_visible_sheet_is_its_own_page(self):
        def build(book):
            fees(book)
            second = book.create_sheet("Hostel")
            second.append(["Block", "Rent"])
            second.append(["A", 12000])

        pages, metadata = workbook_pages(build)

        self.assertEqual([p.metadata["sheet"] for p in pages], ["Fees", "Hostel"])
        self.assertTrue(pages[1].text.startswith("Sheet: Hostel"))
        self.assertEqual(metadata["sheets"], 2)

    def test_blank_rows_separate_tables_on_one_sheet(self):
        def build(book):
            sheet = book.active
            sheet.append(["Fee schedule 2024"])
            sheet.append([])
            sheet.append(["Item", "Amount"])
            sheet.append(["Tuition", 85000])
            sheet.append(["Exam", 2500])
            sheet.append([])
            sheet.append([])
            sheet.append(["Hostel", "Rent"])
            sheet.append(["Block A", 12000])
            sheet.append(["Block B", 11000])

        (page,), _ = workbook_pages(build)

        self.assertEqual(len(page.tables), 2)
        self.assertEqual(page.tables[0].rows[0], ["Item", "Amount"])
        self.assertEqual(page.tables[1].rows[0], ["Hostel", "Rent"])
        self.assertIn("Fee schedule 2024", page.text)

    def test_a_long_sheet_is_paged_as_one_table_running_on(self):
        def build(book):
            sheet = book.active
            sheet.title = "Marks"
            sheet.append(["Name", "Marks", "Grade"])
            for number in range(1, 1101):
                sheet.append([f"S{number}", number % 100, "A"])

        pages, _ = workbook_pages(build)

        self.assertEqual(len(pages), 3)
        self.assertIn("(sheet rows 1-500)", pages[0].text)
        self.assertIn("(sheet rows 501-1000)", pages[1].text)
        self.assertIn("(sheet rows 1001-1101)", pages[2].text)
        self.assertEqual(pages[0].tables[0].rows[0], ["Name", "Marks", "Grade"])
        # The later pages are the rest of that table: no header of their own
        # (LAYOUT_ANALYSIS writes them under the first page's labels), first
        # row data. Sheet row 501 is data row 500.
        for page in pages[1:]:
            table = page.tables[0]
            self.assertTrue(table.continues)
            self.assertEqual(table.header, [])
        self.assertEqual(pages[1].tables[0].rows[0][0], "S500")
        self.assertEqual(pages[1].tables[0].column_ids, (0, 1, 2))

    def test_the_cell_cap_cuts_the_file_off_and_says_so(self):
        def build(book):
            sheet = book.active
            sheet.append(["A", "B", "C"])
            for number in range(100):
                sheet.append([number, number, number])

        with mock.patch.object(spreadsheet, "MAX_CELLS", 30):
            pages, metadata = workbook_pages(build)

        self.assertTrue(metadata["truncated"])
        self.assertIn("Not read", pages[-1].text)

    def test_a_long_run_of_blank_rows_ends_the_sheet(self):
        """The guard against a "used range" declared as the whole grid: once
        this many rows in a row are blank, what follows is not read."""

        def build(book):
            sheet = book.active
            sheet.append(["Only", "Row"])
            sheet.append(["a", "b"])
            sheet.cell(row=100, column=1, value="late")

        with mock.patch.object(spreadsheet, "MAX_BLANK_RUN", 10):
            (page,), _ = workbook_pages(build)

        self.assertNotIn("late", page.text)
        self.assertEqual(page.tables[0].rows, [["Only", "Row"], ["a", "b"]])

    def test_content_after_a_shorter_gap_is_still_read(self):
        def build(book):
            sheet = book.active
            sheet.append(["Only", "Row"])
            sheet.append(["a", "b"])
            sheet.cell(row=8, column=1, value="after a gap")

        with mock.patch.object(spreadsheet, "MAX_BLANK_RUN", 10):
            (page,), _ = workbook_pages(build)

        self.assertIn("after a gap", page.text)

    def test_a_styled_cell_far_down_does_not_make_it_slow(self):
        """A cell with only a style, five thousand rows down, makes the
        sheet's dimension enormous."""

        def build(book):
            sheet = book.active
            sheet.append(["Only", "Row"])
            sheet.append(["a", "b"])
            sheet.cell(row=5000, column=1).number_format = "0.00"

        started = time.monotonic()
        (page,), _ = workbook_pages(build)

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(page.tables[0].rows, [["Only", "Row"], ["a", "b"]])

    def test_a_very_wide_sheet_is_capped_in_columns(self):
        def build(book):
            sheet = book.active
            sheet.append([f"c{n}" for n in range(8)])
            sheet.append(list(range(8)))

        with mock.patch.object(spreadsheet, "MAX_COLUMNS", 5):
            (page,), _ = workbook_pages(build)

        self.assertEqual(len(page.tables[0].rows[0]), 5)

    def test_an_empty_workbook_has_no_pages(self):
        pages, metadata = workbook_pages(lambda book: None)

        self.assertEqual(pages, [])
        self.assertEqual(metadata["sheets"], 0)

    def test_an_unreadable_workbook_is_a_corrupt_document(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "bad.xlsx")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/workbook.xml", "this is not xml")

            with self.assertRaises(StageError) as caught:
                spreadsheet.extract_xlsx(path)

        self.assertEqual(caught.exception.code, ErrorCode.CORRUPT_DOCUMENT)


def delimited(payload: bytes, name="data.csv", preferred=","):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return spreadsheet.extract_delimited(path, preferred)


class DelimitedTests(SimpleTestCase):
    def test_a_csv_is_one_table_with_blank_and_quoted_cells_intact(self):
        payload = (
            b"Programme,Tuition,Note\n"
            b'B.Tech CSE,85000,"Paid in two, equal parts"\n'
            b"B.Tech ECE,,\n"
            b'M.Tech,95000,"Two\nlines"\n'
        )

        (page,), metadata = delimited(payload)

        self.assertEqual(
            page.tables[0].rows,
            [
                ["Programme", "Tuition", "Note"],
                ["B.Tech CSE", "85000", "Paid in two, equal parts"],
                ["B.Tech ECE", "", ""],
                ["M.Tech", "95000", "Two lines"],
            ],
        )
        self.assertEqual(page.tables[0].source, "csv:sheet")
        self.assertEqual(metadata["delimiter"], ",")
        self.assertNotIn("Sheet:", page.text)

    def test_a_semicolon_file_is_recognised(self):
        payload = b"Course;Fee;Seats\nCS101;5000;60\nCS102;6000;45\n"

        (page,), metadata = delimited(payload)

        self.assertEqual(metadata["delimiter"], ";")
        self.assertEqual(page.tables[0].rows[1], ["CS101", "5000", "60"])

    def test_a_tsv_is_split_on_tabs(self):
        payload = b"Course\tFee\nCS101\t5000\nCS102\t6000\n"

        (page,), metadata = delimited(payload, "data.tsv", preferred="\t")

        self.assertEqual(metadata["delimiter"], "tab")
        self.assertEqual(page.tables[0].rows[0], ["Course", "Fee"])

    def test_a_comma_inside_a_tab_file_does_not_win(self):
        payload = b"Course\tNote\nCS101\tIntro, basics\nCS102\tData, structures\n"

        (page,), metadata = delimited(payload, "data.tsv", preferred="\t")

        self.assertEqual(metadata["delimiter"], "tab")
        self.assertEqual(page.tables[0].rows[1], ["CS101", "Intro, basics"])

    def test_encodings_are_read_as_text(self):
        cases = {
            "utf-8": "Name,City\nAsha,Zürich\nRavi,Pune\n".encode(),
            "utf-8-sig": "Name,City\nAsha,Zürich\nRavi,Pune\n".encode("utf-8-sig"),
            "utf-16": "Name,City\nAsha,Zürich\nRavi,Pune\n".encode("utf-16"),
            "cp1252": "Name,City\nAsha,Zürich\nRavi,Pune\n".encode("cp1252"),
        }
        for encoding, payload in cases.items():
            with self.subTest(encoding=encoding):
                (page,), _ = delimited(payload)

                self.assertEqual(page.tables[0].rows[1], ["Asha", "Zürich"])

    def test_a_long_csv_is_paged_as_one_table_running_on(self):
        lines = ["Name,Marks,Grade"] + [f"S{n},{n % 100},A" for n in range(1, 1101)]

        pages, _ = delimited("\n".join(lines).encode())

        self.assertEqual(len(pages), 3)
        self.assertTrue(pages[1].text.startswith("(sheet rows 501-1000)"))
        self.assertTrue(pages[1].tables[0].continues)
        self.assertEqual(pages[1].tables[0].rows[0], ["S500", "0", "A"])

    def test_the_cell_cap_applies_to_a_csv_too(self):
        lines = ["A,B,C"] + [f"{n},{n},{n}" for n in range(100)]

        with mock.patch.object(spreadsheet, "MAX_CELLS", 30):
            pages, metadata = delimited("\n".join(lines).encode())

        self.assertTrue(metadata["truncated"])
        self.assertIn("Not read", pages[-1].text)

    def test_an_empty_csv_has_no_pages(self):
        pages, _ = delimited(b"\n\n\n")

        self.assertEqual(pages, [])
