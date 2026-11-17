"""Tables kept as text: one labelled line per row (tables/text_rows.py).

A table is no longer stored as a grid. Its rows are written into the page as
"Table 2, row 1: Course Code: 23CS101; Credits: 4", and everything downstream
- the chunker, the answerer's arithmetic, the citation, the brief, field
extraction - reads that one form. These tests pin the form and its readers.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from documents.processing.chunking import splitter
from documents.processing.tables import text_rows
from documents.rag import tables as rag_tables
from documents.rag.llm import ExtractiveAnswerer

NL = chr(10)


def _lines(written):
    return written.text.split(NL)


class WritingTests(SimpleTestCase):
    def test_every_value_is_written_beside_its_column(self):
        written = text_rows.write(
            2,
            [
                ["Course Code", "Course Title", "Credits"],
                ["23CS101", "Engineering Mathematics", "4"],
                ["23CS102", "Applied Physics", "3"],
            ],
            ["Course Code", "Course Title", "Credits"],
        )

        self.assertEqual(
            _lines(written),
            [
                "Table 2, row 1: Course Code: 23CS101; "
                "Course Title: Engineering Mathematics; Credits: 4",
                "Table 2, row 2: Course Code: 23CS102; "
                "Course Title: Applied Physics; Credits: 3",
            ],
        )
        self.assertEqual(written.rows, 2)
        self.assertTrue(written.labelled)
        self.assertEqual(written.columns, ["Course Code", "Course Title", "Credits"])

    def test_empty_cells_are_left_out_not_written_blank(self):
        written = text_rows.write(
            1,
            [["Name", "Phone", "Room"], ["Asha", "", "B12"]],
            ["Name", "Phone", "Room"],
        )
        self.assertEqual(_lines(written), ["Table 1, row 1: Name: Asha; Room: B12"])

    def test_a_title_row_names_every_row_and_a_section_row_the_rows_under_it(self):
        written = text_rows.write(
            3,
            [
                ["IIIrd Year - Semester II", "", ""],
                ["Course Code", "Course Title", "Credits"],
                ["23CS301", "Compilers", "3"],
                ["Practical", "", ""],
                ["23CS3L1", "Compilers Lab", "1.5"],
            ],
            ["IIIrd Year - Semester II", "", ""],
        )

        self.assertEqual(
            _lines(written),
            [
                "Table 3 (IIIrd Year - Semester II), row 1: Course Code: 23CS301; "
                "Course Title: Compilers; Credits: 3",
                "Table 3 (IIIrd Year - Semester II / Practical), row 2: "
                "Course Code: 23CS3L1; Course Title: Compilers Lab; Credits: 1.5",
            ],
        )

    def test_a_two_column_table_of_labels_and_values_is_written_as_such(self):
        """The extractor's guess took "Name | Asha Rao" for the header; written
        as labels, every row would say "Name: Roll No"."""
        written = text_rows.write(
            1,
            [
                ["Name", "Asha Rao"],
                ["Roll No", "21A91A0501"],
                ["Date of birth", "12-03-2003"],
            ],
            ["Name", "Asha Rao"],
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Name: Asha Rao",
                "Table 1, row 2: Roll No: 21A91A0501",
                "Table 1, row 3: Date of birth: 12-03-2003",
            ],
        )

    def test_a_two_column_table_of_figures_under_a_header_is_labelled(self):
        written = text_rows.write(
            1,
            [["Programme", "Fee"], ["CSE", "85000"], ["ECE", "80,000"]],
            ["Programme", "Fee"],
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Programme: CSE; Fee: 85000",
                "Table 1, row 2: Programme: ECE; Fee: 80,000",
            ],
        )

    def test_a_first_row_of_data_is_not_made_the_header(self):
        """ "Professor" turns up again below it: it is a value, not a column
        name, and labelling every row with the first row would be wrong."""
        written = text_rows.write(
            1,
            [
                ["Dr. A Rao", "Professor", "CSE"],
                ["Dr. B Iyer", "Professor", "ECE"],
                ["Ms. C Das", "Assistant Professor", "CSE"],
            ],
        )
        self.assertFalse(written.labelled)
        self.assertEqual(
            _lines(written)[0], "Table 1, row 1: Dr. A Rao; Professor; CSE"
        )

    def test_a_form_whose_first_row_was_guessed_as_header_is_read_as_a_form(self):
        """An SSC certificate: names and a roll number in one column is a form,
        not a column under the name "KARRI SUBHASH"."""
        written = text_rows.write(
            1,
            [
                ["Certified that", "KARRI SUBHASH", ""],
                ["Father's Name", "KARRI CHINNARAO", ""],
                ["Mother's Name", "KARRI MANGA", ""],
                ["Roll No.", "2239110176", "Date of Birth 09/12/2005"],
            ],
            ["Certified that", "KARRI SUBHASH", ""],
            strict=True,
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Certified that: KARRI SUBHASH",
                "Table 1, row 2: Father's Name: KARRI CHINNARAO",
                "Table 1, row 3: Mother's Name: KARRI MANGA",
                "Table 1, row 4: Roll No.: 2239110176; Date of Birth 09/12/2005",
            ],
        )

    def test_two_columns_of_running_text_are_not_paired_as_labels(self):
        """A slide's two text columns side by side: "relative positions of"
        is half a line, not the name of "retroreflectors"."""
        written = text_rows.write(
            1,
            [
                ["relative positions of", "retroreflectors"],
                ["the antennas to a", "time-of-flight; satellite orbits"],
                ["few millimeters and", "(mm precision)"],
            ],
        )
        self.assertFalse(written.labelled)
        self.assertEqual(
            _lines(written)[0], "Table 1, row 1: relative positions of; retroreflectors"
        )

    def test_two_bullet_lists_side_by_side_are_not_paired(self):
        written = text_rows.write(
            1,
            [
                ["• Enhanced Safety", "• Increased Flight Efficiencies"],
                ["• Increased Capacity", "• Increased Schedule Predictability"],
            ],
        )
        self.assertFalse(written.labelled)

    def test_a_layout_table_needs_every_column_named(self):
        rows = [
            ["EGNOS:", "System Design", ""],
            ["SES-5 (PRN 136) Geostationary", "GPS", "GLONASS"],
            ["Astra 5B (PRN 123) Satellites", "Galileo", "BeiDou"],
        ]
        self.assertFalse(text_rows.write(1, rows, rows[0], strict=True).labelled)

    def test_a_section_row_of_an_unlabelled_table_is_just_a_row(self):
        """A syllabus's units: "INTRODUCTION" must not be carried onto the
        UNIT - II row that follows it."""
        written = text_rows.write(
            1,
            [
                ["UNIT - I", "", "Periods: 9L"],
                ["INTRODUCTION", "", ""],
                ["History of nano science, definition of nanometer.", "", ""],
                ["UNIT - II", "", "Periods: 9L"],
            ],
        )
        self.assertNotIn("INTRODUCTION)", written.text)
        self.assertIn("Table 1, row 2: INTRODUCTION", written.text)

    def test_one_repeated_column_name_does_not_lose_the_header(self):
        written = text_rows.write(
            1,
            [["Course Outcomes", "PSO1", "PSO2", "PSO2"], ["CO-1", "2", "3", "3"]],
            ["Course Outcomes", "PSO1", "PSO2", "PSO2"],
        )
        self.assertEqual(
            _lines(written),
            ["Table 1, row 1: Course Outcomes: CO-1; PSO1: 2; PSO2: 3; PSO2: 3"],
        )

    def test_a_guessed_header_that_holds_a_value_is_not_used(self):
        written = text_rows.write(
            1,
            [
                ["Name", "Asha Rao", "Roll No", "21A91A0501"],
                ["Branch", "CSE", "Date", "12-03-2003"],
            ],
            ["Name", "Asha Rao", "Roll No", "21A91A0501"],
        )
        self.assertFalse(written.labelled)
        self.assertEqual(
            _lines(written)[0], "Table 1, row 1: Name; Asha Rao; Roll No; 21A91A0501"
        )

    def test_a_declared_header_is_used_even_when_it_reads_like_figures(self):
        """A chart's series are years: the source said they are the header."""
        written = text_rows.write(
            1,
            [["Category", "2021", "2022"], ["North", "10", "12"]],
            ["Category", "2021", "2022"],
            header_declared=True,
        )
        self.assertEqual(
            _lines(written), ["Table 1, row 1: Category: North; 2021: 10; 2022: 12"]
        )

    def test_a_header_printed_again_is_not_a_row(self):
        written = text_rows.write(
            1,
            [["Name", "Marks"], ["A", "90"], ["Name", "Marks"], ["B", "80"]],
            ["Name", "Marks"],
            header_declared=True,
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Name: A; Marks: 90",
                "Table 1, row 2: Name: B; Marks: 80",
            ],
        )

    def test_rows_can_be_numbered_from_where_a_previous_page_stopped(self):
        written = text_rows.write(
            4,
            [["Name", "Marks"], ["C", "70"]],
            ["Name", "Marks"],
            header_declared=True,
            first_row=501,
        )
        self.assertEqual(_lines(written), ["Table 4, row 501: Name: C; Marks: 70"])

    def test_a_blank_corner_cell_leaves_the_row_key_unlabelled(self):
        written = text_rows.write(
            1,
            [["", "Tuition", "Hostel"], ["CSE", "85000", "60000"]],
            ["", "Tuition", "Hostel"],
        )
        self.assertEqual(
            _lines(written), ["Table 1, row 1: CSE; Tuition: 85000; Hostel: 60000"]
        )


class ReadingTests(SimpleTestCase):
    TEXT = NL.join(
        [
            "Scheme of instruction",
            "",
            "Table 1, row 1: Course: CS301; Marks: 84",
            "Table 1, row 2: Course: CS302; Marks: 69",
            "Table 2 (Semester II), row 1: Course: CS401; Marks: 90",
            "Course outcomes follow.",
        ]
    )

    def test_regions_are_runs_of_one_table_with_exact_spans(self):
        found = text_rows.regions(self.TEXT)

        self.assertEqual(
            [(r.number, r.first_row, r.last_row) for r in found], [(1, 1, 2), (2, 1, 1)]
        )
        for region in found:
            span = self.TEXT[region.start : region.end]
            self.assertTrue(span.startswith(f"Table {region.number}"))
            self.assertFalse(span.endswith(NL))
        self.assertEqual(found[1].context, "Semester II")

    def test_a_line_is_read_back_into_its_pairs(self):
        line = text_rows.parse_line(
            "Table 3 (Year II / Practical), row 2: Course Code: 23CS3L1; Lab; Credits: 1.5"
        )
        self.assertEqual(
            (line.number, line.row, line.context), (3, 2, "Year II / Practical")
        )
        self.assertEqual(
            line.pairs, [("Course Code", "23CS3L1"), ("", "Lab"), ("Credits", "1.5")]
        )

    def test_prose_that_mentions_a_table_is_not_a_row(self):
        self.assertIsNone(text_rows.parse_line("Table 2 shows the fees for 2024."))
        self.assertIsNone(text_rows.parse_line("See Table 2, row 1 for details."))
        self.assertEqual(text_rows.regions("Table 2 lists every fee."), [])

    def test_masking_blanks_rows_and_keeps_every_offset(self):
        masked = text_rows.mask(self.TEXT)

        self.assertEqual(len(masked), len(self.TEXT))
        self.assertEqual(masked.split(NL)[0], "Scheme of instruction")
        self.assertNotIn("CS301", masked)
        self.assertIn("Course outcomes follow.", masked)
        self.assertEqual(masked.count(NL), self.TEXT.count(NL))


def _chunk(text, **metadata):
    return SimpleNamespace(
        id=1,
        text=text,
        metadata={"kind": "TABLE", "layout": "labelled", **metadata},
        document_version_id=7,
        document_version=SimpleNamespace(
            document=SimpleNamespace(title="Fees 2024-25")
        ),
    )


ROWS = NL.join(
    [
        "Table 2, row 4: Programme: B.Tech CSE; Tuition: 85000; Hostel: 60000",
        "Table 2, row 5: Programme: B.Tech ECE; Tuition: 82000; Hostel: 60000",
        "Table 2, row 6: Programme: MBA; Tuition: 95000; Hostel: 60000",
    ]
)


class ReferenceTests(SimpleTestCase):
    def chunk(self):
        return _chunk(ROWS, table_number=2, table_index=1, first_row=4, last_row=6)

    def test_a_citation_names_the_row_its_statement_came_from(self):
        reference = rag_tables.table_reference(
            self.chunk(), "The tuition for B.Tech ECE is 82,000."
        )
        self.assertEqual(reference["rows"], [5])
        self.assertEqual(reference["label"], "Table 2, row 5")
        self.assertTrue(reference["narrowed"])

    def test_a_statement_about_the_whole_table_keeps_every_row(self):
        """The hostel fee is in every row: no one row is THE source."""
        reference = rag_tables.table_reference(
            self.chunk(), "The hostel fee is 60000 for every programme."
        )
        self.assertEqual(reference["label"], "Table 2, rows 4-6")
        self.assertFalse(reference["narrowed"])

    def test_without_a_statement_the_reference_is_the_chunk(self):
        reference = rag_tables.table_reference(self.chunk())
        self.assertEqual((reference["first_row"], reference["last_row"]), (4, 6))

    def test_only_labelled_rows_have_a_reference(self):
        self.assertIsNone(
            rag_tables.table_reference(
                SimpleNamespace(text="x", metadata={"kind": "TABLE"})
            )
        )

    def test_a_small_shared_figure_alone_does_not_pick_a_row(self):
        self.assertEqual(
            rag_tables.rows_supporting(
                "Table 1, row 1: Course: A; Credits: 3"
                + NL
                + "Table 1, row 2: Course: B; Credits: 4",
                "It carries 3 credits.",
            ),
            [],
        )


class ArithmeticTests(SimpleTestCase):
    def test_totals_are_computed_per_labelled_column(self):
        passage = SimpleNamespace(chunk=_chunk(ROWS, table_number=2, table_index=1))
        figures = rag_tables.derived_figures([passage])

        self.assertIn("262000", figures)  # tuition total
        self.assertIn("180000", figures)  # hostel total
        self.assertIn("3", figures)  # rows
        self.assertNotIn("2", figures, "a labelled table has no header row to discount")

    def test_two_chunks_of_one_table_are_summed_together(self):
        first = _chunk(ROWS.split(NL)[0], table_number=2, table_index=1)
        second = _chunk(NL.join(ROWS.split(NL)[1:]), table_number=2, table_index=1)
        figures = rag_tables.derived_figures(
            [SimpleNamespace(chunk=first), SimpleNamespace(chunk=second)]
        )
        self.assertIn("262000", figures)

    def test_a_question_naming_a_column_matches_the_table(self):
        chunk = _chunk(ROWS, table_number=2, columns=["Programme", "Tuition", "Hostel"])
        self.assertTrue(
            rag_tables.table_matches_question("What is the hostel charge?", chunk)
        )


class ChunkTests(SimpleTestCase):
    def test_each_chunk_of_a_long_table_carries_its_rows_and_labels(self):
        rows = [["Course Code", "Course Title", "Credits"]] + [
            [f"23CS{100 + i}", f"Course number {i} with a long title", "3"]
            for i in range(1, 41)
        ]
        text = (
            "Scheme"
            + NL
            + NL
            + text_rows.write(3, rows, rows[0], header_declared=True).text
        )

        chunks = [
            c
            for c in splitter.split_page(5, text, chunk_size=600, overlap=100)
            if c.metadata.get("kind") == "TABLE"
        ]

        self.assertGreater(len(chunks), 3)
        expected_first = 1
        for chunk in chunks:
            self.assertEqual(
                text[chunk.character_start : chunk.character_end], chunk.text
            )
            self.assertTrue(chunk.text.startswith(f"Table 3, row {expected_first}: "))
            self.assertEqual(chunk.metadata["first_row"], expected_first)
            self.assertEqual(
                chunk.metadata["columns"], ["Course Code", "Course Title", "Credits"]
            )
            for line in chunk.text.split(NL):
                self.assertIsNotNone(text_rows.parse_line(line), "a row was cut")
            expected_first = chunk.metadata["last_row"] + 1
        self.assertEqual(expected_first, 41)

    def test_a_row_wider_than_a_chunk_is_cut_between_values(self):
        header = [f"K{i}" for i in range(60)]
        text = text_rows.write(
            1, [header, [f"value-{i}" for i in range(60)]], header, header_declared=True
        ).text

        chunks = splitter.split_page(1, text, chunk_size=300, overlap=50)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.metadata["first_row"], 1)
            self.assertTrue(chunk.text.rstrip(";").endswith(tuple("0123456789")))


class QuotingTests(SimpleTestCase):
    def test_a_labelled_row_is_quoted_whole(self):
        """ "Dr." inside a value is not where the statement ends."""
        line = "Table 1, row 2: Name: Dr. A. Rao; Designation: Professor"
        self.assertEqual(ExtractiveAnswerer._sentences(line), [line])
