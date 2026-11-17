"""Tables kept as text: labels only on evidence (a second review's findings).

One wrong label on a value is worse than none, and a value lost is next. These
tests pin what an independent review of the first version found: forms and
lists whose first row passed for a header, notes and totals labelled as values
of the first column, lone cells stamped as sections, figures hidden behind a
row reference, field extraction that ran into the next value, markdown rules
written as rows, a headline in the brief that was a row.

Inputs are the reviewers' own, through the same code the pipeline runs.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from documents.processing.context import ExtractedPage, IngestionContext
from documents.processing.extractors import compose, spreadsheet
from documents.processing.field_extraction import matching
from documents.processing.field_extraction.rule_extractor import DEFAULT_PACK
from documents.processing.field_extraction.schemas import load_pack
from documents.processing.insights import text as insight_text
from documents.processing.stages import layout_analysis
from documents.processing.tables import detector, text_rows
from documents.processing.tables import service as table_service
from documents.rag import context as ctx
from documents.rag import grounding
from documents.rag import tables as rag_tables
from documents.rag.reranking import RerankedChunk
from documents.tests_rag_tables import make_chunk

NL = chr(10)


def lines_of(written):
    return written.text.split(NL)


def after_prefix(written):
    """Each row line without its "Table 1, row 2: " reference."""
    return [line.split(": ", 1)[1] for line in lines_of(written)]


def extractor_written(rows, *, source="python-docx:table", strict=False):
    """A table as an extractor hands it over (header "auto") and as written."""
    builder = compose.PageBuilder(source=source)
    builder.add_table(rows)
    _, tables = builder.build()
    table = tables[0]
    return text_rows.write(
        1,
        table.rows,
        table.header,
        header_declared=table.header_declared,
        strict=strict,
    )


class FormsAreNotHeadersTests(SimpleTestCase):
    FORMS = {
        "fee receipt": (
            [
                ["Student", "Ravi Kumar"],
                ["Tuition Fee", "85000"],
                ["Exam Fee", "2500"],
                ["Total", "87500"],
            ],
            [
                "Student: Ravi Kumar",
                "Tuition Fee: 85000",
                "Exam Fee: 2500",
                "Total: 87500",
            ],
        ),
        "salary slip": (
            [
                ["Employee", "K Rao"],
                ["Basic Pay", "45000"],
                ["HRA", "9000"],
                ["Net Pay", "54000"],
            ],
            [
                "Employee: K Rao",
                "Basic Pay: 45000",
                "HRA: 9000",
                "Net Pay: 54000",
            ],
        ),
        "result card": (
            [["Branch", "CSE"], ["SGPA", "8.5"], ["CGPA", "8.2"]],
            ["Branch: CSE", "SGPA: 8.5", "CGPA: 8.2"],
        ),
        "admission": (
            [
                ["Name", "Asha Rao"],
                ["Date of Admission", "12/06/2021"],
                ["Date of Birth", "03/04/2003"],
            ],
            [
                "Name: Asha Rao",
                "Date of Admission: 12/06/2021",
                "Date of Birth: 03/04/2003",
            ],
        ),
        "marks summary": (
            [["Name", "Asha Rao"], ["Total Marks", "450"], ["Percentage", "75%"]],
            ["Name: Asha Rao", "Total Marks: 450", "Percentage: 75%"],
        ),
    }

    def test_a_form_is_written_as_its_own_labels_and_values(self):
        """ "Student | Ravi Kumar" over "Tuition Fee | 85000" is not a header
        of "Ravi Kumar" over amounts: each row names its own value."""
        for name, (rows, expected) in self.FORMS.items():
            for strict in (False, True):
                with self.subTest(form=name, strict=strict):
                    written = extractor_written(rows, strict=strict)
                    self.assertEqual(after_prefix(written), expected)

    def test_a_wide_form_is_written_as_its_pairs(self):
        rows = [
            ["Name", "Asha Rao", "Branch", "CSE"],
            ["Tuition", "85000", "Hostel", "60000"],
            ["Exam", "2500", "Transport", "12000"],
        ]
        for strict in (False, True):
            written = extractor_written(rows, strict=strict)
            self.assertEqual(
                after_prefix(written),
                [
                    "Name: Asha Rao; Branch: CSE",
                    "Tuition: 85000; Hostel: 60000",
                    "Exam: 2500; Transport: 12000",
                ],
            )

    def test_a_list_whose_first_row_is_all_words_has_no_header(self):
        """A register continued on a page without its header: its first student
        is a row, not the names of three columns."""
        cases = {
            "results": [
                ["Asha Rao", "AB", "AB"],
                ["Ravi Kumar", "45", "60"],
                ["Meena S", "50", "70"],
                ["Kiran P", "38", "55"],
            ],
            "contacts": [
                ["Dr. K Rao", "Principal", "Office"],
                ["Asha", "Professor", "9848012345"],
                ["Ravi", "Lecturer", "9848012346"],
            ],
        }
        for name, rows in cases.items():
            for strict in (False, True):
                with self.subTest(list=name, strict=strict):
                    written = extractor_written(rows, strict=strict)
                    self.assertFalse(written.labelled)
                    self.assertEqual(
                        lines_of(written)[0],
                        "Table 1, row 1: " + "; ".join(rows[0]),
                    )

    def test_two_columns_that_name_nothing_are_written_as_they_are(self):
        """ "Year | Intake" is a header, or it is a form; which cannot be told,
        so no label is claimed: "Year: Intake" would be a wrong one."""
        written = extractor_written(
            [["Year", "Seats"], ["2021", "60"], ["2022", "70"]], strict=True
        )
        self.assertNotIn("Year: Seats", written.text)

    def test_real_headers_are_still_labels(self):
        cases = {
            "programme fee": [["Programme", "Fee"], ["CSE", "85000"], ["ECE", "80000"]],
            "name designation": [
                ["Name", "Designation"],
                ["Asha Rao", "Professor"],
                ["Ravi Kumar", "Lecturer"],
            ],
            "intake": [
                ["Programme", "Intake", "Fee"],
                ["B.Tech CSE", "120", "85000"],
                ["B.Tech ECE", "60", "80000"],
            ],
            "periods": [
                ["Course Code", "Course Title", "L", "T", "P", "Credits"],
                ["23CS101", "Engineering Mathematics", "3", "1", "0", "4"],
                ["23CS102", "Applied Physics", "3", "0", "0", "3"],
            ],
        }
        for name, rows in cases.items():
            with self.subTest(table=name):
                written = extractor_written(rows)
                self.assertTrue(written.labelled)
                self.assertIn(rows[0][0] + ": " + rows[1][0], written.text)

    def test_lab_and_four_digit_codes_are_codes_and_keep_the_header(self):
        rows = [
            ["Course Code", "Course Title", "L", "T", "P", "Credits"],
            ["23CS101", "Engineering Mathematics", "3", "1", "0", "4"],
            ["23CS102", "Applied Physics", "3", "0", "0", "3"],
            ["23CS103", "Programming in C", "3", "0", "0", "3"],
            ["23CS1L1", "Applied Physics Lab", "0", "0", "3", "1.5"],
            ["23CS1L2", "C Programming Lab", "0", "0", "3", "1.5"],
            ["23CS1L3", "Workshop", "0", "0", "3", "1.5"],
        ]
        written = extractor_written(rows)
        self.assertEqual(
            lines_of(written)[0],
            "Table 1, row 1: Course Code: 23CS101; Course Title: Engineering "
            "Mathematics; L: 3; T: 1; P: 0; Credits: 4",
        )
        self.assertEqual(len(lines_of(written)), 6)
        for code in ("23CS1L1", "18CS42", "21CSL38", "CS301", "CO-2"):
            self.assertEqual(text_rows._kind(code), "code", code)

    def test_measures_are_not_codes(self):
        for measure in ("10cm/s", "20ns", "10-20m", "2nd"):
            self.assertEqual(text_rows._kind(measure), "text", measure)


class NotesAndSectionsTests(SimpleTestCase):
    HEADER = ["Course Code", "Course Title", "Credits"]

    def written(self, rows, header=None, **options):
        header = self.HEADER if header is None else header
        return text_rows.write(
            1, [header, *rows], header, header_declared=True, **options
        )

    def test_a_note_is_not_a_value_of_the_first_column(self):
        notes = [
            "Mathematics is common to all branches",
            "Note: Students must register for one MOOC course in consultation",
            "* Mandatory course, no credits",
        ]
        for note in notes:
            with self.subTest(note=note):
                written = self.written(
                    [
                        ["23CS101", "Mathematics", "4"],
                        [note, "", ""],
                        ["23CS102", "Physics", "3"],
                    ]
                )
                self.assertEqual(
                    lines_of(written)[1], "Table 1, row 2: " + note, written.text
                )
                # ... and it names no section for the row after it.
                self.assertTrue(lines_of(written)[2].startswith("Table 1, row 3: "))

    def test_a_total_or_a_remark_is_a_row_not_a_section(self):
        written = self.written(
            [
                ["23CS101", "Mathematics", "4"],
                ["23CS102", "Physics", "3"],
                ["Total", "", ""],
                ["23CS201", "Chemistry", "3"],
            ]
        )
        self.assertEqual(lines_of(written)[2], "Table 1, row 3: Total")
        self.assertTrue(lines_of(written)[3].startswith("Table 1, row 4: Course Code"))

        remarks = text_rows.write(
            1,
            [
                ["S.No", "Name", "Marks"],
                ["1", "Asha", "91"],
                ["2", "Ravi", "88"],
                ["Absent", "", ""],
                ["3", "Meena", "79"],
            ],
            ["S.No", "Name", "Marks"],
            header_declared=True,
        )
        self.assertEqual(lines_of(remarks)[2], "Table 1, row 3: Absent")
        self.assertEqual(
            lines_of(remarks)[3], "Table 1, row 4: S.No: 3; Name: Meena; Marks: 79"
        )

    def test_a_code_alone_in_its_row_keeps_its_column_label(self):
        written = self.written(
            [
                ["23CS101", "Mathematics", "4"],
                ["23CS103", "", ""],
                ["23CS104", "Chemistry", "3"],
            ]
        )
        self.assertEqual(lines_of(written)[1], "Table 1, row 2: Course Code: 23CS103")
        self.assertNotIn("(23CS103)", written.text)

    def test_a_value_with_blanks_beside_it_keeps_its_label(self):
        written = text_rows.write(
            1,
            [
                ["Programme", "Tuition"],
                ["B.Tech CSE", "85000"],
                ["B.Tech ECE", ""],
            ],
            ["Programme", "Tuition"],
            header_declared=True,
        )
        self.assertEqual(lines_of(written)[1], "Table 1, row 2: Programme: B.Tech ECE")

    def test_a_group_heading_among_people_is_not_one_of_them(self):
        """A department's name between staff is a heading: the rows under it
        are about it, and it is nobody's Name."""
        header = ["Name", "Designation", "Qualification"]
        written = text_rows.write(
            1,
            [
                header,
                ["Department of Computer Science", "", ""],
                ["Asha Rao", "Professor", "Ph.D"],
                ["CSE", "", ""],
                ["Ravi Kumar", "Assistant Professor", "M.Tech"],
            ],
            header,
            header_declared=True,
        )
        self.assertEqual(
            lines_of(written),
            [
                "Table 1 (Department of Computer Science), row 1: Name: Asha Rao; "
                "Designation: Professor; Qualification: Ph.D",
                "Table 1 (Department of Computer Science), row 2: CSE",
                "Table 1 (Department of Computer Science), row 3: Name: Ravi Kumar; "
                "Designation: Assistant Professor; Qualification: M.Tech",
            ],
        )

    def test_group_rows_of_a_two_column_table_are_sections(self):
        header = ["Programme", "Fee"]
        written = text_rows.write(
            1,
            [
                header,
                ["Undergraduate", ""],
                ["B.Tech CSE", "85000"],
                ["B.Tech ECE", "80000"],
                ["Postgraduate", ""],
                ["M.Tech", "95000"],
                ["Fees are per annum", ""],
            ],
            header,
            header_declared=True,
        )
        self.assertEqual(
            lines_of(written),
            [
                "Table 1 (Undergraduate), row 1: Programme: B.Tech CSE; Fee: 85000",
                "Table 1 (Undergraduate), row 2: Programme: B.Tech ECE; Fee: 80000",
                "Table 1 (Postgraduate), row 3: Programme: M.Tech; Fee: 95000",
                # A note at the end is about all of it.
                "Table 1, row 4: Fees are per annum",
            ],
        )

    def test_a_title_is_not_demoted_by_a_section_of_another_kind(self):
        written = text_rows.write(
            1,
            [
                ["Semester I", "", ""],
                self.HEADER,
                ["23CS101", "Mathematics", "4"],
                ["23CS102", "Physics", "3"],
                ["Practical", "", ""],
                ["23CS111", "Physics Lab", "1.5"],
            ],
            self.HEADER,
        )
        self.assertEqual(
            [line.split(", row")[0] for line in lines_of(written)],
            [
                "Table 1 (Semester I)",
                "Table 1 (Semester I)",
                "Table 1 (Semester I / Practical)",
            ],
        )

    def test_a_title_of_the_same_kind_as_a_later_section_is_one_of_them(self):
        written = text_rows.write(
            1,
            [
                ["Semester I", "", ""],
                self.HEADER,
                ["23CS101", "Mathematics", "4"],
                ["Semester II", "", ""],
                ["23CS201", "Statistics", "3"],
            ],
            self.HEADER,
        )
        self.assertEqual(
            [line.split(", row")[0] for line in lines_of(written)],
            ["Table 1 (Semester I)", "Table 1 (Semester II)"],
        )


class MarkdownTablesTests(SimpleTestCase):
    MARKDOWN = NL.join(
        [
            "# Credits",
            "",
            "| Course | Credits |",
            "|:-------|--------:|",
            "| Mathematics | 4 |",
            "| Physics | 3 |",
            "",
            "Total credits must be at least 7.",
        ]
    )

    def test_the_rule_under_the_header_is_not_a_row(self):
        (table,) = detector.detect_tables(1, self.MARKDOWN)
        self.assertEqual(
            table.rows,
            [["Course", "Credits"], ["Mathematics", "4"], ["Physics", "3"]],
        )
        self.assertTrue(table.header_declared)
        self.assertEqual(table.header, ["Course", "Credits"])
        self.assertTrue(detector.verify(table, self.MARKDOWN))

    def test_a_markdown_table_is_written_under_its_own_header(self):
        table_service.reset_detector_cache()
        self.addCleanup(table_service.reset_detector_cache)
        context = IngestionContext(
            document_version=None,
            file_path="",
            pages=[ExtractedPage(page_number=1, text=self.MARKDOWN)],
        )

        layout_analysis._run_tables(context)

        self.assertEqual(
            context.pages[0].text,
            NL.join(
                [
                    "# Credits",
                    "",
                    "Table 1, row 1: Course: Mathematics; Credits: 4",
                    "Table 1, row 2: Course: Physics; Credits: 3",
                    "",
                    "Total credits must be at least 7.",
                ]
            ),
        )

    def test_a_pipe_table_without_a_rule_is_still_guessed_not_declared(self):
        text = NL.join(["Name | Marks", "Asha | 91", "Ravi | 88"])
        (table,) = detector.detect_tables(1, text)
        self.assertFalse(table.header_declared)


class GroundingReferenceTests(SimpleTestCase):
    """ "Table 2, row 3" is where a value is; the figures after it are checked."""

    def facts(self, answer):
        return grounding._answer_facts(answer)

    def test_a_reference_is_not_a_figure(self):
        self.assertEqual(
            self.facts("Applied Physics carries 4 credits (Table 2, row 3) [1]."),
            {"4"},
        )
        self.assertEqual(
            self.facts("Table 3 (Semester II), rows 4-6: 4 credits [1]."), {"4"}
        )
        self.assertEqual(self.facts("Rows 1, 3 and 5 carry 4 credits [1]."), {"4"})

    def test_a_figure_after_a_reference_is_still_checked(self):
        cases = {
            "As per Table 2, row 2, 75 seats are available [1].": "75",
            "In Table 2, row 3, 2,500 students are enrolled [1].": "2500",
            "Table 2, rows 1 and 2, 95000 and 90000 are the fees [1].": "95000",
            "Table 2 (Rs. 95,000 per year) [1].": "95000",
        }
        for answer, figure in cases.items():
            with self.subTest(answer=answer):
                found = self.facts(answer)
                self.assertTrue(
                    any(figure in fact for fact in found),
                    f"{figure!r} not among {found}",
                )

    def table_context(self, rows, columns):
        text = NL.join(rows)
        chunk = make_chunk(
            1,
            text,
            title="Fee structure",
            header=(),
            layout="labelled",
            table_number=2,
            first_row=1,
            last_row=len(rows),
            columns=list(columns),
        )
        item = RerankedChunk(chunk=chunk, score=0.9, signals={})
        return ctx.build([item], semantic_scores=True, budget=10**9, max_passages=5)

    ROWS = [
        "Table 2, row 1: Programme: B.Tech CSE; Intake: 120; Fee: 85000",
        "Table 2, row 2: Programme: B.Tech ECE; Intake: 60; Fee: 80000",
    ]
    COLUMNS = ["Programme", "Intake", "Fee"]

    def test_an_invented_figure_beside_a_reference_is_rejected(self):
        context = self.table_context(self.ROWS, self.COLUMNS)
        for answer in (
            "B.Tech ECE has an intake of 75 seats [1].",
            "As per Table 2, row 2, 75 seats are available in B.Tech ECE [1].",
            "Table 2, rows 1 and 2, 95000 and 90000 are the fees [1].",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(grounding.validate(answer, context).ok)

    def test_a_correct_answer_that_cites_its_row_is_accepted(self):
        context = self.table_context(self.ROWS, self.COLUMNS)
        for answer in (
            "B.Tech ECE has an intake of 60 seats (Table 2, row 2) [1].",
            "As per Table 2, row 2, the fee for B.Tech ECE is 80000 [1].",
        ):
            with self.subTest(answer=answer):
                report = grounding.validate(answer, context)
                self.assertTrue(report.ok, report.failures)

    def test_a_row_number_is_not_support_for_a_figure(self):
        context = self.table_context(
            ["Table 2, row 3: Course: Applied Physics; Credits: 4"],
            ["Course", "Credits"],
        )
        self.assertFalse(
            grounding.validate("Applied Physics carries 3 credits [1].", context).ok
        )
        self.assertTrue(
            grounding.validate("Applied Physics carries 4 credits [1].", context).ok
        )


def passage_of(text, **metadata):
    chunk = make_chunk(
        1, text, header=(), layout="labelled", table_number=1, **metadata
    )
    return SimpleNamespace(chunk=chunk)


class DerivedFiguresTests(SimpleTestCase):
    def test_a_form_has_a_total_over_its_values(self):
        written = text_rows.write(
            1,
            [["Tuition Fee", "85000"], ["Exam Fee", "2000"], ["Hostel Fee", "60000"]],
        )
        self.assertEqual(
            after_prefix(written),
            ["Tuition Fee: 85000", "Exam Fee: 2000", "Hostel Fee: 60000"],
        )
        figures = rag_tables.derived_figures([passage_of(written.text)])
        self.assertIn("147000", figures)
        self.assertIn("85000", figures)  # the highest

    def test_a_table_without_labels_may_be_counted_with_or_without_its_header(self):
        """Its first row is a header or a player; both counts are honest."""
        names = ["Asha Rao", "Ravi Kumar", "Meena Sharma", "John Paul", "Sita Devi"]
        teams = ["Royals", "Kings", "Royals", "Titans", "Kings"]
        roles = ["Batter", "Bowler", "Batter", "Keeper", "Bowler"]
        rows = [["Player", "Team", "Role"]] + [
            [name, team, role]
            for name, team, role in zip(names, teams, roles, strict=True)
        ]
        written = text_rows.write(1, rows, strict=True)
        self.assertFalse(written.labelled)
        figures = rag_tables.derived_figures([passage_of(written.text)])
        self.assertIn("5", figures)  # the players, if the first row is a header
        self.assertIn("6", figures)  # the rows, if it is not

    def test_columns_of_a_table_without_labels_line_up(self):
        """Empty cells keep their place, so the column of credits is one
        column: its total is derived."""
        written = text_rows.write(
            1,
            [
                ["23CS101", "Mathematics", "3", "1", "4"],
                ["23CS102", "Physics", "3", "", "3"],
                ["23CS103", "Programming", "2", "", "3"],
                ["23CS104", "Workshop", "", "", "2"],
            ],
            plain=True,
        )
        figures = rag_tables.derived_figures([passage_of(written.text)])
        self.assertIn("12", figures)  # 4 + 3 + 3 + 2


class FieldsInTablesTests(SimpleTestCase):
    SCHEMA = load_pack(DEFAULT_PACK).for_document_type("SYLLABUS")

    def found(self, text):
        matches = matching.find_fields(self.SCHEMA, [(1, text)])
        return {
            match.field_name: match
            for match in matches
            if match.status == matching.FOUND
        }

    def test_a_form_row_gives_each_field_its_own_value(self):
        text = NL.join(
            [
                "Syllabus",
                "",
                "Table 1, row 1: Course Code: CS101; Course Title: Engineering "
                "Mathematics; Credits: 4; Semester: II",
            ]
        )
        found = self.found(text)
        self.assertEqual(found["course_code"].value, "CS101")
        # The value ends where its cell ends: not "... Mathematics Credits: 4".
        self.assertEqual(found["course_title"].value, "Engineering Mathematics")
        self.assertEqual(found["credits"].value, "4")
        self.assertEqual(found["semester"].value, "II")
        # Every span is a span of the stored text.
        for match in found.values():
            span = match.primary
            self.assertEqual(text[span.character_start : span.character_end], span.text)

    def test_a_form_of_one_field_per_row_is_read(self):
        text = NL.join(
            [
                "Table 1, row 1: Course Code: CS101",
                "Table 1, row 2: Credits: 4",
                "Table 1, row 3: Semester: II",
            ]
        )
        found = self.found(text)
        self.assertEqual(
            {name: match.value for name, match in found.items()},
            {"course_code": "CS101", "credits": "4", "semester": "II"},
        )

    def test_a_table_whose_labels_recur_gives_no_field(self):
        text = NL.join(
            f"Table 1, row {n}: Course Code: CS10{n}; Credits: {n}" for n in range(1, 5)
        )
        self.assertEqual(self.found(text), {})

    def test_one_repeated_label_hides_only_its_own_rows(self):
        text = NL.join(
            [
                "Table 1, row 1: Name: Asha",
                "Table 1, row 2: Course Code: CS101",
                "Table 1, row 3: Name: Ravi",
                "Table 1, row 4: Credits: 4",
            ]
        )
        found = self.found(text)
        self.assertEqual(found["course_code"].value, "CS101")
        self.assertEqual(found["credits"].value, "4")

    def test_the_view_is_the_length_of_the_text(self):
        text = NL.join(
            [
                "Intro",
                "Table 1, row 1: Course Code: CS101; Credits: 4",
                "Table 2, row 1: Course Code: CS201; Credits: 3",
                "Table 2, row 2: Course Code: CS202; Credits: 3",
                "After",
            ]
        )
        self.assertEqual(len(text_rows.field_view(text)), len(text))


class BriefAndRowsTests(SimpleTestCase):
    def test_a_row_line_is_not_a_heading(self):
        page = NL.join(
            [
                "Admission details",
                "",
                "Table 1, row 1: Student Name: Asha Rao",
                "Table 1, row 2: Roll No: 21A91A0501",
                "Table 1, row 3: Branch: CSE",
                "",
                "Table 2, row 1: Course Code: CS101; Course Title: Maths; Credits: 4",
            ]
        )
        headings = [s.text for s in insight_text.segment([(1, page)]) if s.is_heading]
        self.assertEqual([h for h in headings if h.startswith("Table ")], [])


def run_sheet(rows):
    """A sheet through the real builder and the real layout stage."""
    pages = spreadsheet.build_pages(
        rows, name="S", source="openpyxl:sheet", source_version="x", metadata={}
    )
    context = IngestionContext(
        document_version=None,
        file_path="",
        pages=[
            ExtractedPage(page_number=n, text=page.text, tables=page.tables)
            for n, page in enumerate(pages, start=1)
        ],
    )
    with override_settings(DOCUMENT_TABLE_DETECTOR=""):
        table_service.reset_detector_cache()
        layout_analysis._run_tables(context)
    table_service.reset_detector_cache()
    return [page.text for page in context.pages]


class PlainSheetContinuationTests(SimpleTestCase):
    def test_a_column_empty_on_the_next_page_keeps_its_place(self):
        """A register without a header: the hours column is filled on the
        first page and empty on the second. Page two's rows keep its slot, so
        the credits are in the same column on both pages."""
        per_page = spreadsheet.ROWS_PER_PAGE
        rows = [
            [f"23CS{n:04d}", f"Course {n}", "1" if n <= per_page else "", "4"]
            for n in range(1, per_page + 4)
        ]

        first, second = run_sheet(rows)

        last = per_page
        self.assertIn(
            f"Table 1, row {last}: 23CS{last:04d}; Course {last}; 1; 4", first
        )
        # Page two goes on from row 501, and the empty slot keeps credits where
        # they were.
        nxt = per_page + 1
        self.assertIn(f"Table 1, row {nxt}: 23CS{nxt:04d}; Course {nxt}; ; 4", second)
