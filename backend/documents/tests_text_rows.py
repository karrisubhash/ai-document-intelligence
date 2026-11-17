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
        """Labels and values side by side, twice per row: a form, read as
        one - not a header of "Asha Rao" over "CSE"."""
        written = text_rows.write(
            1,
            [
                ["Name", "Asha Rao", "Roll No", "21A91A0501"],
                ["Branch", "CSE", "Date", "12-03-2003"],
            ],
            ["Name", "Asha Rao", "Roll No", "21A91A0501"],
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Name: Asha Rao; Roll No: 21A91A0501",
                "Table 1, row 2: Branch: CSE; Date: 12-03-2003",
            ],
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


class ReviewFindingsTests(SimpleTestCase):
    """What an independent review of this change found, each pinned."""

    def test_a_list_of_names_does_not_lend_its_first_row_as_labels(self):
        written = text_rows.write(
            1,
            [
                ["Asha Rao", "Computer Science", "Placement Coordinator"],
                ["Ravi Kumar", "Electronics", "Sports Secretary"],
                ["Meena Sharma", "Mechanical", "Cultural Secretary"],
            ],
            strict=True,
        )
        self.assertFalse(written.labelled)
        self.assertEqual(
            _lines(written)[0],
            "Table 1, row 1: Asha Rao; Computer Science; Placement Coordinator",
        )

    def test_column_names_over_text_columns_are_still_a_header(self):
        written = text_rows.write(
            1,
            [
                ["Command", "Description", "Syntax"],
                ["DROP", "Delete objects from the database", "DROP TABLE t;"],
                [
                    "ALTER",
                    "Alter the structure of the database",
                    "ALTER TABLE t ADD c;",
                ],
            ],
        )
        self.assertEqual(
            _lines(written)[0],
            "Table 1, row 1: Command: DROP; Description: Delete objects from the "
            "database; Syntax: DROP TABLE t;",
        )

    def test_a_wide_form_is_written_as_its_pairs(self):
        written = text_rows.write(
            1,
            [
                ["Name", "KARRI SUBHASH", "Father's Name", "K APPA RAO"],
                ["Branch", "CSE", "Year", "III"],
                ["Roll No", "21A91A0501", "Date of Birth", "01-01-2003"],
            ],
            ["Name", "KARRI SUBHASH", "Father's Name", "K APPA RAO"],
        )
        self.assertEqual(
            _lines(written),
            [
                "Table 1, row 1: Name: KARRI SUBHASH; Father's Name: K APPA RAO",
                "Table 1, row 2: Branch: CSE; Year: III",
                "Table 1, row 3: Roll No: 21A91A0501; Date of Birth: 01-01-2003",
            ],
        )

    def test_a_note_under_a_table_is_kept_as_a_row(self):
        header = ["Course Code", "Course Title", "Credits"]
        written = text_rows.write(
            1,
            [
                header,
                ["23CS101", "Mathematics", "4"],
                ["L - Lecture, T - Tutorial", "", ""],
            ],
            header,
            header_declared=True,
        )
        self.assertEqual(
            _lines(written)[-1], "Table 1, row 2: L - Lecture, T - Tutorial"
        )

    def test_sections_nest_and_replace_their_own_kind(self):
        header = ["Course Code", "Course Title", "Credits"]
        written = text_rows.write(
            1,
            [
                header,
                ["Semester I", "", ""],
                ["Theory", "", ""],
                ["23CS101", "Mathematics", "4"],
                ["Practical", "", ""],
                ["23CS1L1", "Physics Lab", "1.5"],
                ["Semester II", "", ""],
                ["Theory", "", ""],
                ["23CS201", "Discrete Mathematics", "4"],
            ],
            header,
            header_declared=True,
        )
        self.assertEqual(
            [line.split(", row")[0] for line in _lines(written)],
            [
                "Table 1 (Semester I / Theory)",
                "Table 1 (Semester I / Practical)",
                "Table 1 (Semester II / Theory)",
            ],
        )

    def test_a_day_at_the_top_is_not_a_title_over_the_next_day(self):
        written = text_rows.write(
            1,
            [
                ["Monday", "", ""],
                ["Time", "Subject", "Room"],
                ["9:00", "Maths", "Room 1"],
                ["Tuesday", "", ""],
                ["9:00", "Chemistry", "Room 3"],
            ],
        )
        self.assertNotIn("Monday / Tuesday", written.text)
        self.assertIn("(Tuesday), row 2: Time: 9:00; Subject: Chemistry", written.text)

    def test_a_lone_name_in_a_list_is_a_row_not_a_section(self):
        header = ["Name", "Department", "Marks"]
        written = text_rows.write(
            1,
            [
                header,
                ["Asha Rao", "CSE", "91"],
                ["Ravi Kumar", "", ""],
                ["Meena S", "ECE", "88"],
            ],
            header,
            header_declared=True,
        )
        self.assertIn("Table 1, row 2: Name: Ravi Kumar", written.text)
        self.assertNotIn("(Ravi Kumar)", written.text)

    def test_an_unlabelled_row_keeps_every_value_in_its_column(self):
        written = text_rows.write(
            1,
            [
                ["23CS101", "Mathematics", "3", "1", "4"],
                ["23CS104", "Workshop", "", "", "2"],
            ],
            plain=True,
        )
        self.assertEqual(_lines(written)[1], "Table 1, row 2: 23CS104; Workshop; ; ; 2")
        line = text_rows.parse_line(_lines(written)[1])
        self.assertEqual(
            [value for _, value in line.pairs], ["23CS104", "Workshop", "", "", "2"]
        )

    def test_a_label_with_a_colon_in_it_reads_back_whole(self):
        header = ["Course", "Hours (L: T: P)"]
        written = text_rows.write(
            1, [header, ["Maths", "3-1-0"]], header, header_declared=True
        )
        line = text_rows.parse_line(_lines(written)[0])
        self.assertEqual(line.pairs, [("Course", "Maths"), ("Hours (L:T:P)", "3-1-0")])

    def test_a_row_reference_is_not_a_figure_of_the_table(self):
        from documents.rag import grounding

        passage = "Table 2, row 3: Course: Applied Physics; Credits: 4"
        self.assertNotIn("3", grounding._support_facts(passage))
        self.assertIn("4", grounding._support_facts(passage))
        answer = "Table 2, row 3 says Applied Physics carries 4 credits [1]."
        self.assertEqual(grounding._answer_facts(answer), {"4"})

    def test_a_form_stays_readable_to_field_extraction(self):
        text = NL.join(
            [
                "Table 1, row 1: Course Code: CS101",
                "Table 1, row 2: Credits: 4",
                "",
                "Table 2, row 1: Course Code: 23CS101; Credits: 4",
                "Table 2, row 2: Course Code: 23CS102; Credits: 3",
            ]
        )
        view = text_rows.field_view(text)
        self.assertEqual(len(view), len(text))
        self.assertIn("Course Code: CS101", view)
        self.assertIn("Credits: 4", view.split(NL)[1])
        self.assertNotIn("Table 1, row", view)
        # A table whose labels recur on every row gives no one field.
        self.assertNotIn("23CS101", view)


class RealHeaderShapesTests(SimpleTestCase):
    """Headers from real documents that a narrower rule lost, and data rows
    that a looser one would take for headers."""

    def labelled(self, rows, **options):
        return text_rows.write(1, rows, rows[0], **options)

    def test_names_over_figures_with_units(self):
        """A GNSS comparison: "GLONASS" over "10cm/s" and "20ns"."""
        written = self.labelled(
            [
                ["Parameter", "NAVSTAR GPS", "GLONASS", "Galileo", "Beidou1/2"],
                ["Position", "10-20m", "10-20m", "~10-20m", "~10-20m"],
                ["Velocity", "10cm/s", "10cm/s", "20cm/s", "20/40cm/s"],
                ["Time", "0.1 µs (15ns)", "20ns", "20ns", "20/50ns"],
            ]
        )
        self.assertIn(
            "Table 1, row 2: Parameter: Velocity; NAVSTAR GPS: 10cm/s", written.text
        )

    def test_a_header_in_capitals(self):
        written = self.labelled(
            [
                ["OPEN ELECTIVE", "SOFTWARE", "EMERGING COURSE"],
                ["OE-1", "DBMS", "Introduction to Artificial Intelligence"],
                [
                    "OE-2",
                    "Data Structures through Python",
                    "Introduction to Machine Learning",
                ],
            ]
        )
        self.assertIn("OPEN ELECTIVE: OE-1; SOFTWARE: DBMS", written.text)

    def test_outcome_codes_under_their_name_with_dashes_beside(self):
        written = self.labelled(
            [
                ["Course Outcomes", "PSO1", "PSO2", "PSO3"],
                ["CO-1", "-", "-", "-"],
                ["CO-2", "2", "-", "-"],
                ["CO-3", "-", "1", "-"],
            ]
        )
        self.assertIn("Table 1, row 2: Course Outcomes: CO-2; PSO1: 2", written.text)

    def test_an_abbreviation_is_not_the_end_of_a_sentence(self):
        written = self.labelled(
            [
                ["Notice Date", "Company", "City", "No. Of"],
                ["06/22/2015", "Maxim Integrated Product", "San Jose", "150"],
                ["06/30/2015", "McGraw-Hill Education", "Monterey", "137"],
            ]
        )
        self.assertIn("No. Of: 150", written.text)

    def test_period_names_over_amounts(self):
        written = self.labelled(
            [
                ["项 目", "2018 年 1-3 月", "2017 年 1-3 月", "增减%"],
                ["管理费用", "7,764,789.98", "15,753,158.14", "-50.71%"],
                ["财务费用", "7,667.69", "403,265.89", "-98.10%"],
            ]
        )
        self.assertIn("2018 年 1-3 月: 7,764,789.98", written.text)

    def test_a_first_row_shaped_like_the_rows_below_is_data(self):
        written = self.labelled(
            [
                ["Room 101", "Lab 1", "Ground"],
                ["Room 102", "Lab 2", "First"],
                ["Room 103", "Lab 3", "Second"],
            ]
        )
        self.assertFalse(written.labelled)
        self.assertEqual(_lines(written)[0], "Table 1, row 1: Room 101; Lab 1; Ground")

    def test_a_code_alone_in_its_row_is_a_row_not_a_section(self):
        header = ["Course Outcomes", "PSO1", "PSO2", "PSO3"]
        written = text_rows.write(
            1,
            [
                header,
                ["CO-1", "", "", "1"],
                ["CO-2", "", "", ""],
                ["CO-3", "", "", "2"],
            ],
            header,
            header_declared=True,
        )
        self.assertIn("Table 1, row 2: Course Outcomes: CO-2", written.text)
        self.assertNotIn("(CO-2)", written.text)


class SheetContinuationTests(SimpleTestCase):
    """A long sheet through the real builder and the real layout stage."""

    def run_sheet(self, rows):
        from django.test.utils import override_settings

        from documents.processing.context import ExtractedPage, IngestionContext
        from documents.processing.extractors import spreadsheet
        from documents.processing.stages import layout_analysis
        from documents.processing.tables import service as table_service

        pages = spreadsheet.build_pages(
            rows, name="S", source="openpyxl:sheet", source_version="x", metadata={}
        )
        context = IngestionContext(
            document_version=None,
            file_path="",
            pages=[
                ExtractedPage(page_number=number, text=page.text, tables=page.tables)
                for number, page in enumerate(pages, start=1)
            ],
        )
        with override_settings(DOCUMENT_TABLE_DETECTOR=""):
            table_service.reset_detector_cache()
            layout_analysis._run_tables(context)
        table_service.reset_detector_cache()
        return [page.text for page in context.pages]

    def test_the_next_page_keeps_the_labels_title_and_count(self):
        from documents.processing.extractors import spreadsheet

        per_page = spreadsheet.ROWS_PER_PAGE
        rows = [["Library catalogue 2024", "", ""], ["Title", "Author", "Publisher"]]
        rows += [
            [f"Book {i}", f"Writer {i}", f"House {i}"] for i in range(1, per_page + 3)
        ]

        first, second = self.run_sheet(rows)

        self.assertIn(
            "Table 1 (Library catalogue 2024), row 1: Title: Book 1; "
            "Author: Writer 1; Publisher: House 1",
            first,
        )
        # Rows 1-498 of the table are on the first page, under its title and
        # header; the second page goes on from row 499, labelled alike.
        row = per_page - 1
        self.assertIn(
            f"Table 1 (Library catalogue 2024), row {row}: Title: Book {row}; "
            f"Author: Writer {row}",
            second,
        )

    def test_a_column_empty_on_the_next_page_does_not_shift_the_labels(self):
        from documents.processing.extractors import spreadsheet

        per_page = spreadsheet.ROWS_PER_PAGE
        rows = [["Code", "Phone", "Credits"]]
        rows += [
            [f"C{i}", "98480" if i < per_page else "", "3"]
            for i in range(1, per_page + 3)
        ]

        _, second = self.run_sheet(rows)

        self.assertIn(f"Table 1, row {per_page}: Code: C{per_page}; Credits: 3", second)

    def test_a_blank_row_at_the_page_break_starts_a_new_table(self):
        from documents.processing.extractors import spreadsheet

        per_page = spreadsheet.ROWS_PER_PAGE
        rows = [["Code", "Title", "Credits"]]
        rows += [[f"C{i}", f"Course {i}", "4"] for i in range(1, per_page)]
        rows += [
            [],
            ["Name", "Dept", "Phone"],
            ["Asha", "CSE", "98480"],
            ["Ravi", "ECE", "98481"],
        ]

        _, second = self.run_sheet(rows)

        self.assertIn("Table 2, row 1: Name: Asha; Dept: CSE; Phone: 98480", second)
        self.assertNotIn("Table 1", second)

    def test_a_guessed_sheet_header_is_checked(self):
        rows = [
            ["Asha Rao", "Mechanical", "Pune"],
            ["Ravi Kumar", "Civil", "Delhi"],
            ["Meena S", "Mechanical", "Chennai"],
            ["John P", "ECE", "Pune"],
        ]
        (text,) = self.run_sheet(rows)
        self.assertIn("Table 1, row 1: Asha Rao; Mechanical; Pune", text)
