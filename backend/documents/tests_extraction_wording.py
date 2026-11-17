"""Words a PDF gets wrong (extractors/wording.py) and where layout reads them.

The cases are the ones found in real uploads: a LaTeX report that breaks
"inspira-" / "tion" across lines, a syllabus whose converter drew
"Adieselenginehasafuelinjectionsystem" without a single space, and slides
whose bullets are Wingdings glyphs. Every case that must NOT change - a
compound, a French word, a name, an identifier, a link - is here too, because a
repair is only worth having if it never damages a correct word. An independent
review found the first version of this repair rewriting names ("Srinivasan"
became "Sri niv as an") and opening on one real word ("histocompatibility");
the lists below are what it found, so that it cannot happen again.
"""

import gzip
import os
import tempfile
import time
from unittest import mock

from django.test import SimpleTestCase

from documents import pdf_fixtures as fx
from documents.processing.extractors import geometry, pdf, wording


def run(*pages):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.pdf")
        fx.write(path, pages)
        return pdf.extract_page_results(path)


GLUED = "Adieselenginehasafuelinjectionsystemwithafuelpumpthatdelivers1000bar"


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

    def test_a_line_that_was_only_the_word_end_disappears(self):
        self.assertEqual(
            wording.dehyphenate(["the inspira-", "tion"]), ["the inspiration"]
        )

    def test_a_capitalised_continuation_is_never_joined(self):
        lines = ["Retrieval-", "Augmented Generation"]

        self.assertEqual(wording.dehyphenate(lines), lines)

    def test_a_join_that_would_change_the_word_is_left_as_drawn(self):
        """vis-a-vis is not "visa-vis", a go-to is not a "goto", a to-do not a
        "todo", and "short-but-sweet" is not "short-but"."""
        for lines in (
            ["The university's position vis-", "a-vis the state government"],
            ["This is the go-", "to reference for students"],
            ["Add it to the to-", "do list before Friday"],
            ["Both short-", "but not long-term loans"],
            ["both pre-", "and post-processing steps"],
            ["the first, where-", "as the second"],
            ["a plan, not a pro-", "versus a con"],
        ):
            with self.subTest(lines=lines):
                self.assertEqual(wording.dehyphenate(lines), lines)

    def test_a_join_is_not_made_by_scanning_the_whole_line(self):
        """The first version searched every line with a regex that is quadratic
        in the length of a run of letters: 56 s for one 60,000-letter line."""
        # No hyphen at the end: nothing to look for. And one that ends in a
        # hyphen after a long run broken by a digit, which is what makes an
        # unbounded `[A-Za-z]+-$` search quadratic.
        for first in ("a" * 60_000, "a" * 60_000 + "1-"):
            lines = [first, "and a second line"]

            started = time.monotonic()
            self.assertEqual(wording.dehyphenate(lines), lines)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_a_continuation_that_is_itself_hyphenated_is_left_as_drawn(self):
        lines = ["The well-", "known-issue list is long"]

        self.assertEqual(wording.dehyphenate(lines), lines)

    def test_a_continuation_of_one_or_two_letters_is_left_as_drawn(self):
        lines = ["a 4K ultra-", "hd screen with HDR"]

        self.assertEqual(wording.dehyphenate(lines), lines)

    def test_in_a_page_the_join_happens_inside_a_paragraph_only(self):
        page = fx.Page().lines(
            72,
            100,
            [
                "His guidance encouraged students to con-",
                "tribute to the ecosystem.",
            ],
        )
        page.lines(72, 160, ["A new paragraph ends with inspira-"])
        page.lines(72, 220, ["tion on another."])

        (result,) = run(page)

        self.assertIn("students to contribute", result.text)
        self.assertIn("ends with inspira-", result.text)


class GluedWordTests(SimpleTestCase):
    def test_a_run_that_can_only_be_glued_text_is_evidence_and_is_split(self):
        self.assertTrue(wording.has_glued_words([GLUED]))
        self.assertEqual(
            wording.split_glued(GLUED),
            "A diesel engine has a fuel injection system with a fuel pump that "
            "delivers 1000 bar",
        )

    def test_a_long_run_is_evidence_even_when_its_long_words_are_uncommon(self):
        """'portability' and 'usability' are rarer than any fragment could be,
        and fifty-seven letters in eleven listed words is no accident."""
        self.assertEqual(
            wording.split_glued(
                "weightandportabilityaffecttheusabilityofanaircompressorin",
                gated=False,
            ),
            "weight and portability affect the usability of an air compressor in",
        )

    def test_punctuation_that_ends_a_split_run_gets_its_space(self):
        self.assertEqual(
            wording.split_glued("DepartmentofMechanicalEngineering,ANITS."),
            "Department of Mechanical Engineering, ANITS.",
        )
        self.assertEqual(
            wording.split_glued(
                "Localreferencereceiverssenddatatoacentrallocationattheairport.This"
            ),
            "Local reference receivers send data to a central location at the "
            "airport. This",
        )

    def test_words_glued_at_their_capitals_in_a_list_are_split(self):
        self.assertEqual(
            wording.split_glued(
                "Prerequisite:EngineeringMathematics,EngineeringMechanics,"
                "BasicThermodynamics"
            ),
            "Prerequisite: Engineering Mathematics, Engineering Mechanics, "
            "Basic Thermodynamics",
        )

    def test_a_heading_in_capitals_needs_a_sentence_in_it(self):
        self.assertEqual(
            wording.split_glued("CYCLESANDANALYSIS"), "CYCLES AND ANALYSIS"
        )
        for heading in (
            "ELECTRODEPOSITION",
            "AIRCOMPRESSORS",
            "NAVSTARGPS",
            "TOTALENERGIES",
            # A sentence in capitals, but under sixteen letters: as likely a name.
            "WATERANDPOWERS",
        ):
            with self.subTest(heading=heading):
                self.assertEqual(wording.split_glued(heading), heading)

    def test_capitals_are_evidence_only_from_twenty_four_letters(self):
        """Five words in twenty capitals read like a name as much as a sentence."""
        self.assertFalse(wording.has_glued_words(["THESTUDENTSOFTHEHULL"]))
        self.assertTrue(wording.has_glued_words(["THESTUDENTSOFTHEHULLANDTHEMASTER"]))

    def test_a_run_longer_than_any_sentence_is_not_even_looked_at(self):
        text = "thestudentsofthehull" * 30
        with mock.patch.object(wording, "_segment", wraps=wording._segment) as segment:
            self.assertFalse(wording.has_glued_words([text]))
            self.assertEqual(wording.split_glued(text), text)
        segment.assert_not_called()

    def test_a_citation_beside_glued_words_does_not_stop_their_repair(self):
        """In glued text a parenthesis is only a citation: it guards the run it
        touches (a call or an argument), not every run in the token."""
        self.assertEqual(
            wording.split_glued(
                "affected(Tepas,1993).Generally,veryfewcopingstrategieshavebeenfoundtowork"
            ),
            "affected(Tepas,1993).Generally, very few coping strategies have been "
            "found to work",
        )
        # The run beside the parenthesis is the one that stays.
        self.assertEqual(
            wording.split_glued("(numberofstudentsenrolledinthecourse)"),
            "(numberofstudentsenrolledinthecourse)",
        )

    def test_without_evidence_on_the_page_only_evidence_is_split(self):
        # "thepotentialcauses" is not evidence in itself (18 letters, three
        # words): on a page that has none, it stays as drawn.
        self.assertEqual(
            wording.split_glued("thepotentialcauses", gated=False),
            "thepotentialcauses",
        )
        self.assertEqual(
            wording.split_glued("thepotentialcauses", gated=True),
            "the potential causes",
        )
        self.assertEqual(
            wording.split_glued(GLUED, gated=False), wording.split_glued(GLUED)
        )

    def test_a_split_must_read_like_words_drawn_together(self):
        like = wording._sentence_like
        self.assertTrue(like(["the", "potential", "causes"]))
        # Only short function words, which are fragments of longer words.
        self.assertFalse(like(["accident", "a", "list"]))
        self.assertFalse(like(["cinema", "to", "graphical"]))
        # A suffix at the end makes it a derived word.
        self.assertFalse(like(["the", "potential", "ness"]))
        # A scrap of one or two letters that is no function word.
        self.assertFalse(like(["the", "x", "causes"]))
        self.assertFalse(like(["the", "ay", "causes"]))

    def test_a_short_content_word_must_be_very_common_a_long_one_need_not(self):
        # 'hull' and 'dial' are four-letter words about as common as the
        # fragments of longer words: not enough on their own...
        self.assertEqual(
            wording.split_glued("thehullofthedial", gated=True), "thehullofthedial"
        )
        # ...but a long word cannot be a fragment, however rare it is.
        self.assertEqual(
            wording.split_glued("theimplicationsof", gated=True),
            "the implications of",
        )
        # And five words, twenty letters, is evidence whatever the words.
        self.assertEqual(
            wording.split_glued("thestudentsofthehull", gated=False),
            "the students of the hull",
        )

    def test_a_full_stop_before_a_lower_case_word_is_not_a_sentence_end(self):
        self.assertEqual(
            wording.split_glued("Understandandsolvepuzzlesfromthebook.andmore"),
            "Understand and solve puzzles from the book.andmore",
        )

    def test_code_with_evidence_in_it_does_not_open_the_gate(self):
        for code in (
            "numberofstudentsenrolledinthecourse=60;",
            "System.out.println(numberofstudentsenrolledinthecourse)",
            "/home/student/numberofstudentsenrolledinthecourse.txt",
        ):
            with self.subTest(code=code):
                self.assertFalse(wording.has_glued_words([code]))

    def test_a_real_word_the_list_lacks_does_not_open_the_gate(self):
        """The first version opened on 'histocompatibility' ('his to
        compatibility') and rewrote the rest of the page."""
        for word in (
            "histocompatibility",
            "Histocompatibility",
            "indistinguishability",
            "majoritarianism",
            "cinematographic",
            "educationalists",
            "constitutionalists",
            "experimentalists",
            "ballistocardiographic",
            "chlorprophenpyridamine",
            "numberofstudents",
            "reverseofnumber",
            "lengthofthestring",
            "#weareinthistogether",
            "Totalement",
            "prioritaires",
            "intoxications",
            "Halliburton",
            "flightcrew",
        ):
            with self.subTest(word=word):
                self.assertFalse(wording.has_glued_words([word]))

    def test_names_and_derived_words_survive_on_a_page_with_glued_text(self):
        """The first version cut 'Srinivasan' into 'Sri niv as an'."""
        for word in (
            "Srinivasan",
            "Venkatesan",
            "Balagurusamy",
            "Narasimhan",
            "Parthasarathy",
            "Chakravarthy",
            "Krishnamoorthy",
            "Subrahmanyam",
            "Srivastava",
            "Davanagere",
            "Khandelwal",
            "weightages",
            "specialisations",
            "visualisations",
            "intoxications",
            "experimentations",
            "prioritaires",
            "municipalité",
            "responsabilité",
            "Haematochezia",
            "photogrammetric",
            "milliarcsecond",
            "Visakhapatnam",
            "dynamometer",
            "polytropic",
            "isochronism",
            "explainable",
            "PowerPoint",
            "SalesForce",
            "InputStreamReader",
            "PreparedStatement",
            "NumberFormatException",
            "IndexOutOfBoundsException",
            "karrisubhash643@gmail.com",
            "https://nptel.ac.in/courses/112104114",
            "www.mechanicalengineeringworld.com",
            "forms.gle/registerfortheevent",
            "System.out.println(numberofstudents);",
            "/home/student/labs/datasetloader.py",
            "googleresearch.blogspot.co.uk",
        ):
            with self.subTest(word=word):
                self.assertEqual(wording.split_glued(word, gated=True), word)

    def test_codes_stuck_to_glued_words_are_not_cut_apart(self):
        """Spaces go at the edges of a split run, never inside "23ME5111"."""
        self.assertEqual(
            wording.split_glued("23ME5111Understandandsolvepuzzlerelatedquestions"),
            "23ME5111 Understand and solve puzzle related questions",
        )
        self.assertEqual(
            wording.split_glued("CO1:Understandtheconceptsofthermodynamics"),
            "CO1: Understand the concepts of thermodynamics",
        )
        self.assertEqual(
            wording.split_glued("23ME4120:Understandandsolvepuzzlerelatedquestions"),
            "23ME4120: Understand and solve puzzle related questions",
        )

    def test_a_formula_or_ordinal_next_to_glued_words_is_left_alone(self):
        for text, kept in (
            ("TheconcentrationofH2SO4inthesolutionisreduced", "H2SO4"),
            ("ThesongwasstoredasanMP3fileforthestudents", "MP3"),
            ("Thestudentsofthe2ndsemestermustsubmitthereport", "2nd"),
        ):
            with self.subTest(text=text):
                self.assertIn(kept, wording.split_glued(text))

    def test_a_number_stuck_to_a_split_run_is_its_own_word(self):
        self.assertEqual(
            wording.split_glued("outputis150hpat6000rpm,andtheengineneedstoachieve"),
            "outputis150hpat6000rpm, and the engine needs to achieve",
        )

    def test_a_glued_page_is_repaired_and_a_clean_one_is_not(self):
        glued = fx.Page().lines(72, 100, [GLUED, "thepotentialcauses of failure"])
        clean = fx.Page().lines(72, 100, ["thepotentialcauses of failure"])

        repaired, untouched = run(glued, clean)

        self.assertIn("A diesel engine has a fuel injection system", repaired.text)
        self.assertIn("the potential causes of failure", repaired.text)
        self.assertTrue(repaired.metadata.get("glued_words_split"))
        self.assertIn("thepotentialcauses of failure", untouched.text)
        self.assertNotIn("glued_words_split", untouched.metadata)

    def test_cells_of_a_glued_page_are_repaired_too(self):
        page = fx.Page().text(72, 60, GLUED)
        page.grid(
            72,
            100,
            [120, 300],
            [
                ["Code", "Course"],
                ["23ME4120", "Understandandsolvepuzzlerelatedquestions"],
            ],
        )

        (result,) = run(page)

        (table,) = result.tables
        self.assertEqual(
            table.rows[1], ["23ME4120", "Understand and solve puzzle related questions"]
        )

    def test_a_page_with_a_real_rare_word_and_names_is_left_exactly_alone(self):
        page = fx.Page().lines(
            72,
            100,
            [
                "Unit 3: the major histocompatibility complex (MHC) is studied.",
                "Prof. K. Srinivasan and Dr. S. Venkatesan teach the course.",
            ],
        )

        (result,) = run(page)

        self.assertIn("histocompatibility complex", result.text)
        self.assertIn("Srinivasan", result.text)
        self.assertNotIn("glued_words_split", result.metadata)

    def test_a_very_long_run_costs_little_and_is_left_alone(self):
        started = time.monotonic()
        text = "a" * 60_000
        self.assertEqual(wording.split_glued(text), text)
        self.assertFalse(wording.has_glued_words([text]))
        self.assertLess(time.monotonic() - started, 2.0)


class BulletTests(SimpleTestCase):
    WINGDINGS = chr(0xF0A7)
    SYMBOL_ALPHA = chr(0xF061)
    TICK = chr(0xF0FC)
    CROSS = chr(0xF0FB)

    def test_a_symbol_font_bullet_at_the_start_of_a_line_is_a_bullet(self):
        for glyph in (0xF0A7, 0xF0B7, 0xF0D8, 0xF06E, 0xF076):
            with self.subTest(glyph=hex(glyph)):
                self.assertEqual(
                    wording.bullet_line(chr(glyph) + " Static"), "• Static"
                )

    def test_a_tick_or_a_box_at_the_start_of_a_line_is_not_a_bullet(self):
        """Checklists: 'Fee receipt attached' ticked, an option not chosen."""
        for text in (
            self.TICK + " Fee receipt attached",
            self.CROSS + " Signature missing",
            chr(0xF0A8) + " Option A",
            chr(0xF0FE) + " Option B",
        ):
            with self.subTest(text=text):
                self.assertEqual(wording.bullet_line(text), text)

    def test_a_bullet_glued_to_its_text_gets_a_space(self):
        self.assertEqual(wording.bullet_line(self.WINGDINGS + "Static"), "• Static")

    def test_the_same_code_points_elsewhere_stay_as_drawn(self):
        """Greek letters, arrows, ticks and crosses live in the same range."""
        for text in (
            self.SYMBOL_ALPHA + " = 30 degrees",
            "R = 10 " + chr(0xF0B1) + " 0.5 and I = 2 A",
            "required: " + self.TICK + " Yes " + self.CROSS + " No",
            "angle " + self.SYMBOL_ALPHA,
        ):
            with self.subTest(text=text):
                self.assertEqual(wording.bullet_line(text), text)

    def test_lines_of_a_page_get_their_bullets_and_only_those(self):
        def word(text, x0, top):
            return geometry.Word(text=text, x0=x0, x1=x0 + 40, top=top, bottom=top + 10)

        lines = [
            geometry.Line(
                [word(self.WINGDINGS, 72, 100), word("Static", 90, 100)], 100, 110
            ),
            geometry.Line(
                [
                    word("angle", 72, 114),
                    word(self.SYMBOL_ALPHA, 120, 114),
                    word("= 30", 140, 114),
                ],
                114,
                124,
            ),
        ]

        text = geometry._lines_to_text(lines, 10.0)

        self.assertEqual(
            text.splitlines(), ["• Static", "angle " + self.SYMBOL_ALPHA + " = 30"]
        )


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
                self.assertEqual(wording.split_glued(GLUED), GLUED)
                self.assertFalse(wording.has_glued_words([GLUED]))
        finally:
            wording._ranks.cache_clear()

    def test_a_damaged_file_is_the_same_as_a_missing_one(self):
        """A truncated or corrupt download must switch the repairs off; it
        used to raise out of every page and send the whole PDF to the
        fallback reader, with no tables."""
        with tempfile.TemporaryDirectory() as folder:
            with gzip.open(wording.WORD_LIST, "rb") as handle:
                good = gzip.compress(handle.read())
            damaged = {
                "truncated.gz": good[: len(good) // 2],
                "bad-utf8.gz": gzip.compress(b"the of and \xff\xfe broken"),
                "not-gzip.gz": b"plain text, not gzip",
                "empty.gz": b"",
            }
            for name, payload in damaged.items():
                path = os.path.join(folder, name)
                with open(path, "wb") as handle:
                    handle.write(payload)
                wording._ranks.cache_clear()
                try:
                    with self.subTest(file=name):
                        with mock.patch.object(wording, "WORD_LIST", path):
                            with self.assertLogs("adi.extraction", level="ERROR"):
                                self.assertEqual(wording._ranks(), {})
                            self.assertEqual(wording.split_glued(GLUED), GLUED)
                finally:
                    wording._ranks.cache_clear()

    def test_a_page_is_still_read_with_a_damaged_word_list(self):
        page = fx.Page().lines(72, 100, ["A line of ordinary text.", "Another line."])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "damaged.gz")
            with open(path, "wb") as handle:
                handle.write(gzip.compress(b"the of")[:-6])
            wording._ranks.cache_clear()
            try:
                with mock.patch.object(wording, "WORD_LIST", path):
                    with self.assertLogs("adi.extraction", level="ERROR"):
                        (result,) = run(page)
            finally:
                wording._ranks.cache_clear()

        self.assertIn("A line of ordinary text.", result.text)
        self.assertNotIn("fallback_reason", result.metadata)

    def test_a_split_is_remembered_but_cannot_be_changed_by_a_caller(self):
        first = wording._segment("thequickbrownfox")
        first.append("changed")

        self.assertEqual(wording._segment("thequickbrownfox")[-1], "fox")
        self.assertIsNone(wording._segment("zzqxjvkw"))
