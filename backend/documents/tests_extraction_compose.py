"""The shared plumbing of the extraction overhaul.

`PageBuilder` is what lets an extractor hand over a table as cells while the
page text stays the single source of truth: the span it records must
re-slice, cell for cell, out of the text it returns. These tests hold it to
that, then check the two places that consume it - LAYOUT_ANALYSIS, and the
text detector's handling of blank pipe cells.
"""

from django.test import SimpleTestCase, override_settings

from documents.processing.context import ExtractedPage, IngestionContext, PageTable
from documents.processing.extractors import compose
from documents.processing.stages import layout_analysis
from documents.processing.tables import detector
from documents.processing.tables import service as table_service


def _slice(text, table):
    return text[table.character_start : table.character_end]


class PageBuilderTests(SimpleTestCase):
    def test_a_table_span_reslices_to_exactly_its_rows(self):
        page = compose.PageBuilder(source="demo")
        page.add_text("Fee structure 2024")
        stored = page.add_table(
            [["Programme", "Tuition"], ["B.Tech CSE", "85000"], ["B.Tech ECE", "82000"]]
        )
        page.add_text("Payable before the semester begins.")

        text, tables = page.build()

        self.assertTrue(stored)
        self.assertEqual(len(tables), 1)
        self.assertEqual(
            _slice(text, tables[0]),
            "Programme | Tuition\nB.Tech CSE | 85000\nB.Tech ECE | 82000",
        )
        self.assertEqual(tables[0].source, "demo")
        self.assertTrue(
            detector.verify(
                detector.DetectedTable(
                    page_number=1,
                    table_index=0,
                    character_start=tables[0].character_start,
                    character_end=tables[0].character_end,
                    rows=tables[0].rows,
                    separator="PIPE",
                ),
                text,
            )
        )

    def test_a_table_is_set_apart_by_blank_lines(self):
        page = compose.PageBuilder()
        page.add_text("Before")
        page.add_table([["a", "1"], ["b", "2"]])
        page.add_text("After")

        text, _ = page.build()

        self.assertEqual(text, "Before\n\na | 1\nb | 2\n\nAfter")

    def test_two_paragraphs_are_one_newline_apart(self):
        page = compose.PageBuilder()
        page.add_text("First")
        page.add_text("Second")

        self.assertEqual(page.build()[0], "First\nSecond")

    def test_offsets_hold_across_several_tables_and_text(self):
        page = compose.PageBuilder()
        page.add_text("Intro")
        page.add_table([["A", "B"], ["1", "2"]])
        page.add_text("Middle paragraph")
        page.add_table([["C", "D"], ["3", "4"], ["5", "6"]])

        text, tables = page.build()

        self.assertEqual(_slice(text, tables[0]), "A | B\n1 | 2")
        self.assertEqual(_slice(text, tables[1]), "C | D\n3 | 4\n5 | 6")

    def test_blank_cells_stay_in_their_column(self):
        page = compose.PageBuilder()
        page.add_table(
            [["Item", "Qty", "Note"], ["Pens", "10", ""], ["Paper", "", "out of stock"]]
        )

        text, tables = page.build()

        self.assertEqual(
            tables[0].rows,
            [
                ["Item", "Qty", "Note"],
                ["Pens", "10", ""],
                ["Paper", "", "out of stock"],
            ],
        )
        self.assertEqual(
            text, "Item | Qty | Note\nPens | 10 |\nPaper |  | out of stock"
        )

    def test_empty_rows_and_columns_are_removed_but_ragged_rows_are_padded(self):
        page = compose.PageBuilder()
        page.add_table(
            [["Name", "", "Score"], ["", "", ""], ["Asha", None, "91"], ["Ravi"]]
        )

        _, tables = page.build()

        self.assertEqual(
            tables[0].rows, [["Name", "Score"], ["Asha", "91"], ["Ravi", ""]]
        )

    def test_a_single_row_or_column_is_text_not_a_table(self):
        row = compose.PageBuilder()
        self.assertFalse(row.add_table([["Name: Asha", "Date: 3 May"]]))
        self.assertEqual(row.build(), ("Name: Asha | Date: 3 May", []))

        column = compose.PageBuilder()
        self.assertFalse(column.add_table([["First item"], ["Second item"]]))
        self.assertEqual(column.build(), ("First item\nSecond item", []))

    def test_a_single_cell_keeps_its_paragraphs(self):
        page = compose.PageBuilder()
        page.add_table([["Line one\nLine two"]])

        self.assertEqual(page.build()[0], "Line one\nLine two")

    def test_a_cell_stays_on_one_line_and_loses_its_pipe(self):
        page = compose.PageBuilder()
        page.add_table([["Code", "Meaning"], ["A|B", "two\nlines  here"]])

        text, tables = page.build()

        self.assertEqual(tables[0].rows[1], ["A\u00a6B", "two lines here"])
        self.assertEqual(len(_slice(text, tables[0]).split("\n")), 2)

    def test_the_header_is_the_first_row_when_it_is_clearly_not_data(self):
        auto = compose.PageBuilder()
        auto.add_table([["Course", "Marks"], ["CS301", "84"], ["CS302", "69"]])
        self.assertEqual(auto.tables[0].header, ["Course", "Marks"])

        numeric_first = compose.PageBuilder()
        numeric_first.add_table([["2023", "84"], ["2024", "69"]])
        self.assertEqual(numeric_first.tables[0].header, [])

        forced = compose.PageBuilder()
        forced.add_table([["a", "b"], ["c", "d"]], header=True)
        self.assertEqual(forced.tables[0].header, ["a", "b"])

        refused = compose.PageBuilder()
        refused.add_table([["Course", "Marks"], ["CS301", "84"]], header=False)
        self.assertEqual(refused.tables[0].header, [])

    def test_ligatures_and_invisible_characters_are_cleaned(self):
        page = compose.PageBuilder()
        page.add_text("o\ufb03ce \ufb01le\u00adname\u200b and\u00a0more")

        self.assertEqual(page.build()[0], "office filename and more")

    def test_a_zero_width_joiner_is_kept_for_indic_scripts(self):
        word = "\u0915\u094d\u200d\u0937"

        page = compose.PageBuilder()
        page.add_text(word)

        self.assertEqual(page.build()[0], word)

    def test_nul_bytes_are_gone_before_offsets_are_measured(self):
        page = compose.PageBuilder()
        page.add_text("a\x00b")
        page.add_table([["x\x00", "y"], ["1", "2"]])

        text, tables = page.build()

        self.assertNotIn("\x00", text)
        self.assertEqual(_slice(text, tables[0]), "x | y\n1 | 2")

    def test_trailing_spaces_and_runaway_blank_lines_are_trimmed(self):
        page = compose.PageBuilder()
        page.add_text("one   \n\n\n\n\ntwo  \n")

        self.assertEqual(page.build()[0], "one\n\ntwo")

    def test_nothing_added_means_no_content(self):
        page = compose.PageBuilder()
        page.add_text("   \n  ")
        page.add_table([["", ""], ["", ""]])

        self.assertFalse(page.has_content)
        self.assertEqual(page.build(), ("", []))


class PipeCellTests(SimpleTestCase):
    def test_a_blank_pipe_cell_keeps_its_column(self):
        text = "Item | Qty | Note\nPens | 10 |\nPaper |  | out of stock\n"

        tables = detector.detect_tables(1, text)

        self.assertEqual(len(tables), 1)
        self.assertEqual(
            tables[0].rows,
            [
                ["Item", "Qty", "Note"],
                ["Pens", "10", ""],
                ["Paper", "", "out of stock"],
            ],
        )

    def test_outer_pipes_do_not_add_columns(self):
        text = "| Course | Marks |\n| CS301 | 84 |\n| CS302 | 69 |\n"

        tables = detector.detect_tables(1, text)

        self.assertEqual(tables[0].rows[0], ["Course", "Marks"])
        self.assertEqual(tables[0].column_count, 2)

    def test_a_row_of_only_separators_is_not_a_table(self):
        self.assertEqual(detector.detect_tables(1, "| | |\n| | |\n"), [])

    def test_extra_tabs_are_still_padding_not_empty_columns(self):
        text = "Name\t\tFee\nAsha\t\t100\nRavi\t\t200\n"

        tables = detector.detect_tables(1, text)

        self.assertEqual(tables[0].rows[0], ["Name", "Fee"])


def _context(*pages):
    return IngestionContext(
        document_version=None,
        file_path="",
        pages=list(pages),
    )


class StructuredTableStageTests(SimpleTestCase):
    def setUp(self):
        table_service.reset_detector_cache()
        self.addCleanup(table_service.reset_detector_cache)

    def _page(self, number=1, *, trailing=""):
        builder = compose.PageBuilder(source="demo-extractor", source_version="1")
        builder.add_text("Fee structure")
        builder.add_table(
            [["Programme", "Tuition"], ["B.Tech CSE", "85000"], ["B.Tech ECE", ""]]
        )
        if trailing:
            builder.add_text(trailing)
        text, tables = builder.build()
        return ExtractedPage(page_number=number, text=text, tables=tables)

    def test_an_extractor_table_is_written_once_as_labelled_rows(self):
        context = _context(self._page())

        note, metadata = layout_analysis._run_tables(context)

        self.assertEqual(len(context.tables), 1, "the text detector duplicated it")
        table = context.tables[0]
        self.assertEqual(table.source, "demo-extractor")
        self.assertEqual((table.number, table.first_row, table.last_row), (1, 1, 2))
        page = context.pages[0]
        self.assertEqual(
            page.text,
            "Fee structure\n\n"
            "Table 1, row 1: Programme: B.Tech CSE; Tuition: 85000\n"
            "Table 1, row 2: Programme: B.Tech ECE",
        )
        self.assertEqual(
            page.text[table.character_start : table.character_end],
            "Table 1, row 1: Programme: B.Tech CSE; Tuition: 85000\n"
            "Table 1, row 2: Programme: B.Tech ECE",
        )
        self.assertEqual(page.tables, [], "the grid was kept beside the text")
        self.assertEqual(metadata["table_sources"], ["demo-extractor"])
        self.assertEqual(metadata["table_rows"], 2)
        self.assertIn("1 table", note)

    @override_settings(DOCUMENT_TABLE_DETECTOR="")
    def test_an_extractor_table_does_not_need_the_text_detector(self):
        context = _context(self._page())

        note, metadata = layout_analysis._run_tables(context)

        self.assertEqual(len(context.tables), 1)
        self.assertNotIn("skipped", metadata)

    def test_a_table_the_extractor_missed_is_still_found_by_the_detector(self):
        trailing = "Course\tMarks\nCS301\t84\nCS302\t69"
        context = _context(self._page(trailing=trailing))

        layout_analysis._run_tables(context)

        sources = [table.source for table in context.tables]
        self.assertEqual(sources, ["demo-extractor", ""])
        text = context.pages[0].text
        found = context.tables[1]
        self.assertEqual(
            text[found.character_start : found.character_end],
            "Table 2, row 1: Course: CS301; Marks: 84\n"
            "Table 2, row 2: Course: CS302; Marks: 69",
        )

    def test_tables_are_numbered_in_reading_order_across_pages(self):
        context = _context(self._page(1), self._page(2))

        layout_analysis._run_tables(context)

        self.assertEqual(
            [(t.page_number, t.number) for t in context.tables],
            [(1, 1), (2, 2)],
        )
        self.assertIn("Table 2, row 1: Programme: B.Tech CSE", context.pages[1].text)

    def test_the_rest_of_a_sheet_keeps_its_table_number_and_row_count(self):
        """A long sheet split across pages is one table: its second page goes
        on from the row the first page ended at, under the same number."""
        first = compose.PageBuilder(source="sheet")
        first.add_table(
            [["Name", "Marks", "Grade"], ["Asha", "91", "O"], ["Ravi", "78", "A"]],
            header=True,
        )
        second = compose.PageBuilder(source="sheet")
        second.add_table(
            [["Name", "Marks", "Grade"], ["Priya", "88", "A+"]],
            header=True,
            continues=True,
        )
        pages = []
        for number, builder in enumerate((first, second), start=1):
            text, tables = builder.build()
            pages.append(ExtractedPage(page_number=number, text=text, tables=tables))
        context = _context(*pages)

        layout_analysis._run_tables(context)

        self.assertEqual(
            context.pages[1].text,
            "Table 1, row 3: Name: Priya; Marks: 88; Grade: A+",
        )
        self.assertEqual(
            [(t.number, t.first_row, t.last_row) for t in context.tables],
            [(1, 1, 2), (1, 3, 3)],
        )

    def test_a_span_that_does_not_hold_its_cells_is_refused(self):
        page = ExtractedPage(
            page_number=1,
            text="Fee structure\n\nsomething else entirely",
            tables=[
                PageTable(
                    character_start=15,
                    character_end=len("Fee structure\n\nsomething else entirely"),
                    rows=[["Tuition", "999999"], ["Exam", "1"]],
                    source="demo-extractor",
                )
            ],
        )
        context = _context(page)

        note, metadata = layout_analysis._run_tables(context)

        self.assertEqual(context.tables, [])
        self.assertEqual(metadata["tables_rejected"], 1)
        self.assertIn("rejected 1", note.lower())

    @override_settings(DOCUMENT_TABLE_DETECTOR="")
    def test_with_no_detector_and_no_extractor_tables_the_stage_is_skipped(self):
        context = _context(ExtractedPage(page_number=1, text="Just prose."))

        _, metadata = layout_analysis._run_tables(context)

        self.assertTrue(metadata["skipped"])
