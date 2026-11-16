"""Words a PDF gets wrong (extractors/wording.py) and where layout reads them.

The cases are the ones found in real uploads: a LaTeX report that breaks
"inspira-" / "tion" across lines, a syllabus whose converter drew
"Adieselenginehasafuelinjectionsystem" without a single space, and slides
whose bullets are Wingdings glyphs. Every case that must NOT change - a
compound, a French word, a name, a CamelCase identifier - is here too,
because a repair is only worth having if it never damages a correct word.
"""

import os
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import pdf, wording


def run(*pages):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path)


class DehyphenationTests(SimpleTestCase):
    def test_a_word_broken_across_lines_is_joined(self):
        lines = ["his constant inspira-", "tion and vision behind it"]

        self.assertEqual(
            wording.dehyphenate(lines),
            ["his constant inspiration", "and vision behind it"],
        )

    def test_a_hyphenated_compound_keeps_its_hyphen(self):
        for head, tail in (("AI", "powered"), ("open", "source"), ("well", "known")):
            lines = [f"an {head}-", f"{tail} system"]
            self.assertEqual(
                wording.dehyphenate(lines), [f"an {head}-{tail}", "system"]
            )

    def test_a_prefix_half_lets_the_joined_word_decide(self):
        self.assertEqual(
            wording.dehyphenate(["the 5 in-", "put nodes"]), ["the 5 input", "nodes"]
        )

    def test_a_capitalised_continuation_is_never_joined(self):
        lines = ["Retrieval-", "Augmented Generation"]

        self.assertEqual(wording.dehyphenate(lines), lines)

    def test_a_hyphen_left_hanging_on_purpose_stays_hanging(self):
        lines = ["both pre-", "and post-processing steps"]

        self.assertEqual(wording.dehyphenate(lines), lines)

    def test_a_little_word_that_completes_a_word_is_still_joined(self):
        self.assertEqual(
            wording.dehyphenate(["the first, where-", "as the second"]),
            ["the first, whereas", "the second"],
        )

    def test_a_line_that_was_only_the_word_end_disappears(self):
        self.assertEqual(
            wording.dehyphenate(["the inspira-", "tion"]), ["the inspiration"]
        )

    def test_in_a_page_the_join_happens_inside_a_paragraph_only(self):
        page = fx.Page().lines(
            72,
            100,
            ["His guidance encouraged students to con-", "tribute to the ecosystem."],
        )
        page.lines(72, 160, ["A new paragraph ends with inspira-"])
        page.lines(72, 220, ["tion on another."])

        (result,) = run(page)

        self.assertIn("students to contribute", result.text)
        self.assertIn("ends with inspira-", result.text)


class GluedWordTests(SimpleTestCase):
    GLUED = "Adieselenginehasafuelinjectionsystemwithafuelpumpthatdelivers1000bar"

    def test_a_page_that_shows_the_fault_has_its_runs_split(self):
        self.assertTrue(wording.has_glued_words([self.GLUED]))
        self.assertEqual(
            wording.split_glued(self.GLUED),
            "A diesel engine has a fuel injection system with a fuel pump that "
            "delivers 1000 bar",
        )

    def test_punctuation_between_glued_words_gets_its_space(self):
        self.assertEqual(
            wording.split_glued("DepartmentofMechanicalEngineering,ANITS."),
            "Department of Mechanical Engineering, ANITS.",
        )

    def test_long_words_drawn_together_at_their_capitals_are_split(self):
        self.assertEqual(
            wording.split_glued(
                "Prerequisite:EngineeringMathematics,EngineeringMechanics,"
                "BasicThermodynamics"
            ),
            "Prerequisite: Engineering Mathematics, Engineering Mechanics, "
            "Basic Thermodynamics",
        )

    def test_headings_in_capitals_are_split_into_long_words_only(self):
        self.assertEqual(
            wording.split_glued("APPLIEDTHERMODYNAMICS-II"), "APPLIED THERMODYNAMICS-II"
        )
        self.assertEqual(wording.split_glued("NAVSTARGPS"), "NAVSTARGPS")
        # Two common words in capitals, but short enough to be a name.
        self.assertEqual(wording.split_glued("TOTALENERGIES"), "TOTALENERGIES")

    def test_words_that_are_merely_unlisted_are_not_evidence(self):
        for text in (
            "Totalement",
            "prioritaires",
            "experimentations",
            "intoxications",
            "Halliburton",
            "flightcrew",
            "JavaScript",
        ):
            with self.subTest(text=text):
                self.assertFalse(wording.has_glued_words([text]))

    def test_on_a_glued_page_real_words_are_still_left_alone(self):
        for word in (
            "Visakhapatnam",
            "dynamometer",
            "polytropic",
            "isochronism",
            "PowerPoint",
            # Splits only into scraps: "act iv at ions", "Hall i burton".
            "activations",
            "Halliburton",
            # CamelCase: split exactly at its capitals, it is a name.
            "SalesForce",
            # Capitals mark this run's boundaries; one elsewhere cuts a word.
            "MakerchipApp",
            "karrisubhash643@gmail.com",
            "https://nptel.ac.in/courses/112104114",
            "www.mechanicalengineeringworld.com",
        ):
            with self.subTest(word=word):
                self.assertEqual(wording.split_glued(word), word)

    def test_a_glued_page_is_repaired_and_a_clean_one_is_not(self):
        glued = fx.Page().lines(72, 100, [self.GLUED, "Casestudies of engines"])
        clean = fx.Page().lines(72, 100, ["Casestudies of engines"])

        repaired, untouched = run(glued, clean)

        self.assertIn("A diesel engine has a fuel injection system", repaired.text)
        self.assertIn("Case studies of engines", repaired.text)
        self.assertTrue(repaired.metadata.get("glued_words_split"))
        self.assertIn("Casestudies of engines", untouched.text)
        self.assertNotIn("glued_words_split", untouched.metadata)

    def test_cells_of_a_glued_page_are_repaired_too(self):
        page = fx.Page().text(72, 60, self.GLUED)
        page.grid(
            72,
            100,
            [120, 200],
            [["Code", "Course"], ["23ME4120", "APPLIEDTHERMODYNAMICS"]],
        )

        (result,) = run(page)

        (table,) = result.tables
        self.assertEqual(table.rows[1], ["23ME4120", "APPLIED THERMODYNAMICS"])

    def test_without_the_word_list_nothing_changes(self):
        wording._ranks.cache_clear()
        try:
            with self.settings():
                original = wording._ranks
                wording._ranks = lambda: {}
                try:
                    self.assertEqual(wording.split_glued(self.GLUED), self.GLUED)
                    self.assertEqual(
                        wording.dehyphenate(["inspira-", "tion"]), ["inspira-", "tion"]
                    )
                    self.assertFalse(wording.has_glued_words([self.GLUED]))
                finally:
                    wording._ranks = original
        finally:
            wording._ranks.cache_clear()


class BulletTests(SimpleTestCase):
    def test_a_symbol_font_bullet_is_written_as_a_bullet(self):
        self.assertEqual(wording.bullet("\uf0a7"), "\u2022")
        self.assertEqual(wording.bullet("\uf0d8"), "\u2022")
        self.assertEqual(wording.bullet("a"), "a")
        self.assertEqual(wording.bullet("\uf0a7x"), "\uf0a7x")


class WordListTests(SimpleTestCase):
    def test_the_word_list_ships_with_the_code(self):
        self.assertTrue(os.path.exists(wording.WORD_LIST))
        ranks = wording._ranks()
        self.assertGreater(len(ranks), 100_000)
        self.assertEqual(ranks["the"], 0)

    def test_its_licence_ships_beside_it(self):
        licence = os.path.join(
            os.path.dirname(wording.WORD_LIST), "LICENSE-wordninja.txt"
        )
        with open(licence, encoding="utf-8") as handle:
            self.assertIn("MIT License", handle.read())

    def test_a_missing_file_is_logged_and_switches_the_repairs_off(self):
        wording._ranks.cache_clear()
        try:
            with mock.patch.object(wording, "WORD_LIST", "/nonexistent/words.gz"):
                with self.assertLogs("adi.extraction", level="ERROR") as logs:
                    self.assertEqual(wording._ranks(), {})
                self.assertIn("word_list_missing", logs.output[0])
                self.assertEqual(
                    wording.dehyphenate(["inspira-", "tion"]), ["inspira-", "tion"]
                )
        finally:
            wording._ranks.cache_clear()
