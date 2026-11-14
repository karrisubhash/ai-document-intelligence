"""What reads the extracted text must read a table written as text.

Extractors now hand tables over as rows of " | "-separated cells. Everything
downstream that parses lines was written when a table's cells came out
separated by spaces or tabs, and several of those readers allow whitespace, a
colon or a hyphen between a label and its value - but not a pipe. These tests
hold the readers to the same answer whichever way the same table is written,
and hold the spans they report to the invariant everything is built on: what
is stored at [start:end] is what the span says it is.
"""

from django.test import SimpleTestCase

from documents.processing.field_extraction.matching import find_field
from documents.processing.field_extraction.rule_extractor import DEFAULT_PACK
from documents.processing.field_extraction.schemas import load_pack
from documents.processing.insights.entities import extract_entities
from documents.processing.normalize import matchable
from documents.rag import llm

PACK = load_pack(DEFAULT_PACK)


def rule(document_type, name):
    return next(r for r in PACK.schemas[document_type].fields if r.name == name)


class MatchableViewTests(SimpleTestCase):
    def test_cell_separators_become_spaces_and_nothing_else_changes(self):
        text = "Form No | F-12\n| edge |\nx|y and a||b"

        view = matchable(text)

        self.assertEqual(view, "Form No   F-12\n  edge  \nx|y and a||b")

    def test_the_view_is_the_same_length_so_offsets_carry_over(self):
        text = "a | b\n| c |\n\n| | |\nplain text\n"

        self.assertEqual(len(matchable(text)), len(text))

    def test_a_pipe_inside_a_word_or_a_run_of_pipes_is_left_alone(self):
        self.assertEqual(matchable("a|b"), "a|b")
        self.assertEqual(matchable("a||b"), "a||b")
        self.assertEqual(matchable("|"), " ")


class FieldsAcrossLayoutsTests(SimpleTestCase):
    LAYOUTS = {
        "spaces": "Form No   F-12\n",
        "colon": "Form No: F-12\n",
        "pipes": "Field | Value\nForm No | F-12\n",
        "framed pipes": "| Form No | F-12 |\n",
    }

    def test_a_label_beside_its_value_is_found_however_the_table_is_written(self):
        field_rule = rule("FORM", "form_number")

        for name, text in self.LAYOUTS.items():
            with self.subTest(layout=name):
                match = find_field(field_rule, [(1, text)])

                self.assertEqual(match.status, "FOUND")
                self.assertEqual(match.value, "F-12")

    def test_the_span_is_what_the_document_says_at_that_place(self):
        field_rule = rule("FORM", "form_number")
        text = "Field | Value\nForm No | F-12\n"

        match = find_field(field_rule, [(1, text)])

        span = match.spans[0]
        self.assertEqual(text[span.character_start : span.character_end], span.text)
        self.assertEqual(span.text, "F-12")

    def test_a_value_that_runs_across_cells_keeps_the_documents_own_pipe(self):
        """The view hides the separator from the pattern; the stored value and
        span must still be the document's text, separator included."""
        field_rule = rule("FORM", "form_title")
        text = "Application for Leave of absence | Casual leave\n"

        match = find_field(field_rule, [(1, text)])

        span = match.spans[0]
        self.assertEqual(text[span.character_start : span.character_end], span.text)
        self.assertIn("|", span.text)
        self.assertEqual(match.value, "Leave of absence | Casual leave")

    def test_a_label_with_no_value_is_still_not_found(self):
        match = find_field(rule("FORM", "form_number"), [(1, "Form No | \n")])

        self.assertEqual(match.status, "NOT_FOUND")


class EntitiesAcrossLayoutsTests(SimpleTestCase):
    def by_type(self, text):
        return {e.entity_type: e for e in extract_entities([(1, text)])}

    def test_a_deadline_in_a_table_is_a_deadline_as_after_a_colon(self):
        for name, text in {
            "colon": "Last date: 15/07/2024\n",
            "spaces": "Last date   15/07/2024\n",
            "pipes": "Item | When\nLast date | 15/07/2024\n",
        }.items():
            with self.subTest(layout=name):
                found = self.by_type(text)

                self.assertIn("DEADLINE", found)
                self.assertNotIn("DATE", found)

    def test_a_label_carries_no_cell_separator(self):
        found = self.by_type("Item | When\nLast date | 15/07/2024\n")

        self.assertNotIn("|", found["DEADLINE"].label)

    def test_every_entity_span_is_the_stored_text_at_that_place(self):
        text = "Item | Amount\nTuition fee | Rs. 85,000\nLast date | 15/07/2024\n"

        for entity in extract_entities([(1, text)]):
            self.assertEqual(text[entity.start : entity.end], entity.text)

    def test_an_amount_in_a_table_is_found(self):
        found = self.by_type("Tuition fee | Rs. 85,000\n")

        self.assertEqual(found["MONEY"].text, "Rs. 85,000")


class AnswererReadsTablesTests(SimpleTestCase):
    def test_rows_with_blank_cells_are_recognised_as_table_rows(self):
        for row in (
            "Course | Fee | Seats",
            "Pens | 10 |",
            "Paper |  | out of stock",
            "| framed | row |",
        ):
            with self.subTest(row=row):
                self.assertTrue(llm._looks_like_a_table_row(row))

    def test_a_sentence_is_not_a_table_row(self):
        self.assertFalse(
            llm._looks_like_a_table_row("The fee is payable before the semester.")
        )

    def test_table_rows_are_never_joined_into_a_sentence(self):
        text = "Course | Fee\nCS101 | 5000\nlower case row | 12\n"

        self.assertEqual(
            llm._unwrap(text),
            ["Course | Fee", "CS101 | 5000", "lower case row | 12"],
        )

    def test_a_footnote_marker_is_not_mistaken_for_a_citation(self):
        """The answerer reserves [12] for its own citations; the DOCX
        extractor writes footnote markers as [fn1] for that reason."""
        self.assertIsNone(llm.DOCUMENT_BRACKET.search("Enrolment rose[fn1] and [en2]"))
        self.assertIsNotNone(llm.DOCUMENT_BRACKET.search("see clause [12]"))
