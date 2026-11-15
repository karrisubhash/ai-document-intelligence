"""What the third independent review found in the grounding check, fixed.

Two kinds of defect, which pull in opposite directions and are tested as such:

PRECISION. Leniencies this branch added accepted figures no passage states
("one time" supporting a 1; a range supported by its two ends from different
sentences; a citation repaired onto a passage that counts something else).
Each is tightened, and each test below shows the WRONG figure still rejected.

RECALL. Correct answers were thrown away for how they spell a fact (the
user's own figure echoed back, "**1.**" list numbers, "[1-2]", "15-day",
"7:30 PM", "3 lakh"). Each fix is an EQUIVALENCE of two spellings of one fact
and each has a negative test: a different figure is still caught.
"""

import re
import time
from unittest import mock

from django.test import SimpleTestCase

from documents.rag import context as ctx
from documents.rag import facts, grounding, tables
from documents.rag.reranking import RerankedChunk
from documents.tests_rag import chunk
from documents.tests_rag_tables import make_chunk

NEWLINE = chr(10)
EN = "\N{EN DASH}"
EM = "\N{EM DASH}"


def build(*texts, titles=()):
    items = []
    for number, text in enumerate(texts, start=1):
        title = titles[number - 1] if number <= len(titles) else "Doc"
        items.append(
            RerankedChunk(chunk=chunk(number, text, title=title), score=0.9, signals={})
        )
    return ctx.build(items, semantic_scores=True, budget=10**9, max_passages=50)


def check(answer, *texts, titles=(), user_text=""):
    extra = {"user_text": user_text} if user_text else {}
    return grounding.validate(answer, build(*texts, titles=titles), **extra)


class Base(SimpleTestCase):
    def accepted(self, answer, *texts, **kwargs):
        report = check(answer, *texts, **kwargs)
        self.assertTrue(
            report.ok,
            f"{answer!r} rejected: {report.failures} {report.unsupported_facts}",
        )
        return report

    def rejected(self, answer, *texts, figure=None, **kwargs):
        report = check(answer, *texts, **kwargs)
        self.assertFalse(report.ok, f"{answer!r} was accepted")
        if figure is not None:
            self.assertIn(figure, report.unsupported_facts, report.unsupported_facts)
        return report


# --- grounding/F2(a): "one" is an idiom far more often than a quantity -------

LIBRARY = (
    "An undergraduate student may borrow up to four books at one time, and a "
    "postgraduate student up to six books, for a period of fourteen days."
)
GRIEVANCE = (
    "A complainant may be accompanied by one member of the institute "
    "community. An appeal must be lodged with the Director within fifteen "
    "days of the decision being communicated."
)


class BareOneTests(Base):
    def test_one_in_an_idiom_is_not_the_numeral_one(self):
        for text in (
            "No one may attend the examination without the card.",
            "Students may borrow four books at one time.",
            "One of the conditions is attendance.",
            "Residents must respect one another at all times.",
            "A complainant may bring one member of the community.",
            "Only one student may use the room.",
            "Attach one copy of the form to the application.",
            "Each student gets one attempt at the paper.",
        ):
            with self.subTest(text=text):
                self.assertNotIn("1", facts.number_words(text))

    def test_the_other_number_words_in_that_sentence_still_count(self):
        self.assertEqual(
            facts.number_words("up to four books at one time, for fourteen days"),
            {"4", "14"},
        )

    def test_a_compound_with_a_scale_word_is_unambiguously_a_quantity(self):
        for text, numeral in (
            ("within one hundred and eighty days", "180"),
            ("a fine of two thousand rupees", "2000"),
            ("income below one lakh", "100000"),
            ("a grant of one crore", "10000000"),
            ("one hundred percent", "100"),
            ("two thousand five hundred rupees", "2500"),
            ("income below two lakh rupees", "200000"),
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.number_words(text), {numeral})

    def test_one_before_a_unit_of_time_is_a_quantity(self):
        for text in (
            "within one week of the result",
            "one month's notice is required",
            "valid for one year",
            "after one day",
            "at least one hour before the start",
            "every one minute",
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.number_words(text), {"1"})

    def test_a_hallucinated_1_is_not_supported_by_one_time(self):
        for answer in (
            "An undergraduate student may borrow 1 book at a time [1].",
            "An undergraduate student may borrow up to 1 books at one time [1].",
        ):
            with self.subTest(answer=answer):
                self.rejected(answer, LIBRARY, figure="1")

    def test_a_hallucinated_1_is_not_supported_by_one_member(self):
        self.rejected(
            "A complainant may be accompanied by 1 member; an appeal must be "
            "lodged within 1 days [1].",
            GRIEVANCE,
            figure="1",
        )

    def test_the_figures_the_passage_does_state_are_still_supported(self):
        self.accepted(
            "An undergraduate student may borrow up to 4 books for 14 days [1].",
            LIBRARY,
        )
        self.accepted(
            "An appeal must be lodged within 15 days of the decision [1].", GRIEVANCE
        )

    def test_a_wrong_figure_is_still_rejected(self):
        self.rejected(
            "An undergraduate student may borrow up to 7 books at one time [1].",
            LIBRARY,
            figure="7",
        )

    def test_a_passage_that_writes_the_numeral_still_supports_it(self):
        self.accepted(
            "A student may borrow 1 book at a time [1].",
            "A student may borrow 1 book at a time from the library.",
        )

    def test_one_week_in_the_passage_supports_1_week(self):
        self.accepted(
            "Fees are due within 1 week of the notice [1].",
            "Fees are due within one week of the notice being issued.",
        )


class QuantityPhraseTests(Base):
    """ "May borrow up to six." ends the clause: nothing follows to count."""

    def test_a_number_word_after_a_quantifier_is_a_quantity(self):
        for text, numeral in (
            ("Postgraduate students may borrow up to six.", "6"),
            ("Postgraduate students may borrow at least three.", "3"),
            ("No more than ten may attend.", "10"),
            ("Exactly twenty-five are chosen.", "25"),
            ("A maximum of fifteen is allowed.", "15"),
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.number_words(text), {numeral})

    def test_it_is_the_numeral_the_answer_may_state(self):
        self.accepted(
            "A postgraduate student may borrow up to 6 books [1].",
            "Undergraduate students may borrow up to four books at one time. "
            "Postgraduate students may borrow up to six.",
        )

    def test_a_number_word_with_no_quantifier_and_no_noun_is_still_nothing(self):
        for text in (
            "The two of them attended.",
            "Of the six, three were late.",
            "Take the third and the fifth.",
            "At least one of the following is required.",
            "Not more than one may attend.",
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.number_words(text), set())

    def test_a_wrong_figure_is_still_rejected(self):
        self.rejected(
            "A postgraduate student may borrow up to 7 books [1].",
            "Postgraduate students may borrow up to six.",
            figure="7",
        )
        self.rejected(
            "A postgraduate student may borrow 2 books [1].",
            "Postgraduate students may borrow up to six, and the two of them agreed.",
            figure="2",
        )


# --- grounding/F2(b): a range is supported by a range, not by two figures ----

REGULATIONS = (
    "A student who scores less than 40 percent in the end-semester examination "
    "fails that course, irrespective of internal marks."
    + NEWLINE
    + "A student must maintain a minimum of 75 percent attendance in each course."
)


class RangeTests(Base):
    def test_two_unrelated_figures_do_not_make_a_range(self):
        for answer, figure in (
            (
                "Attendance between 40-75 percent may apply for condonation on "
                "medical grounds [1].",
                "40-75",
            ),
            ("Condonation may be sought within 15-75 days [1].", "15-75"),
            ("Marks of 40-75 percent decide the grade [1].", "40-75"),
        ):
            with self.subTest(answer=answer):
                self.rejected(answer, REGULATIONS, figure=figure)

    def test_a_year_pair_from_two_unrelated_sentences_is_not_a_range(self):
        self.rejected(
            "The 2024-2025 batch pays tuition of 85000 per year [1].",
            "Admitted in 2024. Convocation in 2025. The tuition of the batch is "
            "85000 per year.",
            figure="2024-2025",
        )

    def test_the_two_ends_in_two_different_passages_are_not_a_range(self):
        self.rejected(
            "Attendance of 40-75 percent matters [1][2].",
            "The fail mark is 40 percent in every course.",
            "Attendance of 75 percent is the minimum required.",
            figure="40-75",
        )

    def test_a_range_the_passage_states_is_supported_however_it_is_written(self):
        for passage in (
            "Condonation applies to attendance between 65 and 75 percent.",
            "Condonation applies to attendance from 65 to 75 percent.",
            "Condonation applies to attendance of 65-75 percent.",
            "Condonation applies to attendance of 65 - 75 percent.",
            "Condonation applies to attendance of 65" + EN + "75 percent.",
            "Condonation applies to attendance of 65 percent to 75 percent.",
        ):
            with self.subTest(passage=passage):
                self.accepted(
                    "Condonation applies to attendance of 65-75 percent [1].", passage
                )

    def test_an_amount_range_the_passage_states(self):
        self.accepted(
            "Tuition is Rs 1500-2000 per year [1].",
            "Tuition is from Rs 1500 to Rs 2000 per year depending on the course.",
        )
        self.accepted(
            "Tuition is Rs 1500-2000 per year [1].",
            "Tuition is between Rs 1500 and Rs 2000 per year depending on the course.",
        )

    def test_a_range_written_with_an_en_dash_in_the_answer_needs_the_same_support(self):
        self.rejected(
            "Attendance of 40" + EN + "75 percent matters [1].",
            REGULATIONS,
        )
        self.accepted(
            "Condonation applies to attendance of 65" + EN + "75 percent [1].",
            "Condonation applies to attendance between 65 and 75 percent.",
        )

    def test_a_wrong_end_is_still_rejected(self):
        self.rejected(
            "Condonation applies to attendance of 55-75 percent [1].",
            "Condonation applies to attendance between 65 and 75 percent.",
        )

    def test_two_figures_joined_by_a_bare_and_are_not_a_range(self):
        self.rejected(
            "Attendance of 65-75 percent matters [1].",
            "Attendance of 65 percent and a fee of 75 rupees are required.",
        )

    def test_ends_far_apart_are_not_a_range(self):
        self.rejected(
            "Attendance of 65-75 percent matters [1].",
            "Attendance between 65 percent in the theory courses of the first "
            "two semesters and 75 percent in the laboratory courses.",
        )

    def test_a_short_gap_of_long_words_is_still_too_far(self):
        """Three words and a connector, in 42 characters: not "about 40"."""
        gap = " percentage attendance representation and "
        self.assertGreater(len(gap), facts.RANGE_WINDOW)
        self.rejected(
            "Attendance of 65-75 percent matters [1].",
            "Attendance between 65" + gap + "75 percent is required.",
        )
        self.accepted(
            "Attendance of 65-75 percent matters [1].",
            "Attendance between 65 percent attendance and 75 percent is required.",
        )

    def test_a_range_cut_in_two_by_the_end_of_a_passage_is_not_in_one_passage(self):
        self.rejected(
            "Condonation applies to attendance of 65-75 percent [1][2].",
            "Condonation applies to attendance between 65",
            "and 75 percent for medical reasons.",
            figure="65-75",
        )

    def test_a_table_row_is_not_a_range(self):
        self.rejected(
            "The fees are Rs 2500-3000 [1].",
            "Exam fee | 2500 | 3000",
        )

    def test_a_chain_of_three_numbers_is_a_date_or_a_part_number_not_a_range(self):
        """A passage that prints "05 - 06 - 2025" and an answer that quotes it."""
        self.accepted(
            "Fees are due on 05 - 06 - 2025 without exception [1].",
            "Fees are due on 05 - 06 - 2025 without exception.",
        )
        self.assertEqual(
            facts.range_dashes("65 - 75 and 05 - 06 - 2025 and 1-2-3 and 3" + EN + "4"),
            "65-75 and 05 - 06 - 2025 and 1-2-3 and 3-4",
        )

    def test_a_year_range_still_matches_however_it_is_written(self):
        for answer in (
            "For 2024-2025 the tuition is 85000 [1].",
            "For 2024" + EN + "25 the tuition is 85000 [1].",
            "For 2024-25 the tuition is 85000 [1].",
            "For 2024 to 2025 the tuition is 85000 [1].",
        ):
            with self.subTest(answer=answer):
                self.accepted(answer, "Fee Structure 2024-25. Tuition is 85000.")
        self.rejected(
            "For 2025-26 the tuition is 85000 [1].",
            "Fee Structure 2024-25. Tuition is 85000.",
        )

    def test_the_years_a_course_ran(self):
        passage = "The course ran from 2019 to 2021 in two phases."
        self.accepted("The course ran in 2019-2021 [1].", passage)
        self.accepted("The course ran in 2019" + EN + "2021 [1].", passage)
        self.rejected("The course ran in 2019-2022 [1].", passage)


# --- grounding/F1: a repair must not move a figure onto another meaning ------

SCHOLARSHIP = (
    "A scholarship holder must maintain a minimum of 85 percent attendance in "
    "every course."
)
PASS = "A lost bus pass is replaced on payment of 200 rupees at the transport office."
LIBRARY_FINE = (
    "The late fine at the library is 120 rupees per day for residents who "
    "return books late."
)
HOSTEL_CHARGES = "Hostel charges are 1800 rupees per month for every resident."


class RepairMeaningTests(Base):
    def repair(self, answer, *texts):
        context = build(*texts)
        report = grounding.validate(answer, context)
        self.assertFalse(report.ok, report.failures)
        repaired, repairs = grounding.repair_citations(report, context)
        return context, repaired, repairs

    def test_a_figure_that_counts_something_else_is_not_repaired(self):
        for answer, passage in (
            (
                "A scholarship holder must maintain a minimum of 85 percent "
                "attendance [1], and a lost bus pass is replaced after 200 days.",
                PASS,
            ),
            (
                "A scholarship holder must maintain a minimum of 85 percent "
                "attendance [1], and 120 students attend the library late.",
                LIBRARY_FINE,
            ),
            (
                "A scholarship holder must maintain a minimum of 85 percent "
                "attendance [1], and the hostel has 1800 residents.",
                HOSTEL_CHARGES,
            ),
        ):
            with self.subTest(answer=answer):
                context, repaired, repairs = self.repair(answer, SCHOLARSHIP, passage)
                self.assertEqual(repairs, [])
                self.assertFalse(grounding.validate(repaired, context).ok)

    def test_an_honest_omission_is_still_repaired(self):
        context, repaired, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and a lost bus pass is replaced on payment of 200 "
            "rupees.",
            SCHOLARSHIP,
            PASS,
        )

        self.assertEqual(repairs, [("200", 2)])
        self.assertTrue(grounding.validate(repaired, context).ok)

    def test_the_unit_written_another_way_is_still_the_same_unit(self):
        context, repaired, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and replacing a lost bus pass costs Rs 200.",
            SCHOLARSHIP,
            PASS,
        )

        self.assertEqual(repairs, [("200", 2)])
        self.assertTrue(grounding.validate(repaired, context).ok)

    def test_the_same_unit_on_another_subject_is_not_repaired(self):
        """Round two: 'the exam fee is 4500 rupees' was repaired onto a bus pass
        that costs 4500 rupees, because both are rupees and the clause shared
        words with the passage. What the figure is the amount OF must match."""
        _, _, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and the exam fee is 4500 rupees per semester.",
            SCHOLARSHIP,
            "A bus pass costs 4500 rupees per semester and is issued on payment.",
        )

        self.assertEqual(repairs, [])

    def test_a_different_thing_counted_is_not_repaired(self):
        for claim, passage in (
            ("the late fine is 500 days", "The late fine is 500 rupees per week."),
            ("the hostel has 200 students", "The hostel has 200 seats."),
            ("the library holds 500 rupees", "The library holds 500 books."),
        ):
            with self.subTest(claim=claim):
                _, _, repairs = self.repair(
                    "A scholarship holder must maintain a minimum of 85 percent "
                    f"attendance [1], and {claim}.",
                    SCHOLARSHIP,
                    passage,
                )
                self.assertEqual(repairs, [])

    def test_a_date_on_another_subject_is_not_repaired(self):
        _, _, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and hostel registration closes on 15 August 2025.",
            SCHOLARSHIP,
            "A student must register with the cell before 15 August of the final year.",
        )

        self.assertEqual(repairs, [])

    def test_a_time_or_a_range_is_never_repaired(self):
        for claim, passage in (
            ("the office opens at 9:30 am", "The office opens at 9:30 am daily."),
            # A range written with a dash is one figure ("100-500"), and a
            # passage that states it as "between 100 and 500" supports it as a
            # range; it is never attributed by words.
            (
                "fines run 100-500 rupees",
                "Fines run between 100 and 500 rupees.",
            ),
        ):
            with self.subTest(claim=claim):
                _, _, repairs = self.repair(
                    "A scholarship holder must maintain a minimum of 85 percent "
                    f"attendance [1], and {claim}.",
                    SCHOLARSHIP,
                    passage,
                )
                self.assertEqual(repairs, [])

    def test_a_date_is_repaired_only_beside_the_same_words(self):
        passage = "Registration for the placement cell closes on 15 August 2025."
        _, _, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and registration closes on 15 August 2025.",
            SCHOLARSHIP,
            passage,
        )
        self.assertEqual(repairs, [("date:2025-08-15", 2)])

        _, _, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and the placement cell registration review is on "
            "15 August 2025.",
            SCHOLARSHIP,
            passage,
        )
        self.assertEqual(repairs, [])

    def test_a_clause_with_nothing_beside_its_figure_is_not_repaired(self):
        _, _, repairs = self.repair(
            "A scholarship holder must maintain a minimum of 85 percent "
            "attendance [1], and then 200.",
            SCHOLARSHIP,
            PASS,
        )
        self.assertEqual(repairs, [])

    def test_the_support_of_a_passage_is_worked_out_once(self):
        passages = [SCHOLARSHIP] + [
            f"Passage {n} about hostel rules and fee number {n} of the term."
            + " word" * 40
            for n in range(11)
        ]
        context = build(*passages)
        answer = "Hostel fees [1]. " + " ".join(
            f"The fee is {900000 + n}." for n in range(40)
        )
        report = grounding.validate(answer, context)

        with mock.patch.object(
            grounding, "_support_facts", wraps=grounding._support_facts
        ) as spy:
            grounding.repair_citations(report, context)

        self.assertLessEqual(spy.call_count, len(context.passages))


# --- grounding/F4: dashes between numbers, and an interval that is not a citation


class DashAndBracketTests(Base):
    def test_a_dashed_pair_in_the_passage_still_supports_each_end(self):
        for passage, answer in (
            (
                "Batch 2020" + EN + "2024 is a four-year programme.",
                "The batch ran from 2020 to 2024 [1].",
            ),
            (
                "Programme 2019" + EM + "2021 is offered in two phases.",
                "The programme started in 2019 [1] in two phases.",
            ),
            (
                "The examination fee is Rs 2500 "
                + EN
                + " 3000 depending on the course.",
                "The examination fee is Rs 2500 depending on the course [1].",
            ),
            (
                "The bus pass costs 4500" + EN + "5500 rupees per semester.",
                "The bus pass costs between 4500 and 5500 rupees per semester [1].",
            ),
        ):
            with self.subTest(answer=answer):
                self.accepted(answer, passage)

    def test_a_dashed_pair_does_not_support_a_third_figure(self):
        self.rejected(
            "The batch ran from 2020 to 2025 [1].",
            "Batch 2020" + EN + "2024 is a four-year programme.",
            figure="2025",
        )

    def test_the_academic_year_equivalences_survive(self):
        passage = "Campus bus services for the academic year 2024" + EN + "25 follow."
        self.accepted("The academic year 2024-25 has bus services [1].", passage)
        self.accepted("The academic year 2024-2025 has bus services [1].", passage)
        self.rejected("The academic year 2023-24 has bus services [1].", passage)

    def test_a_title_with_a_dash_supports_each_year(self):
        for title in ("Fee Structure 2024" + EN + "2025", "Batch 2020" + EN + "2024"):
            with self.subTest(title=title):
                self.accepted(
                    f"The tuition for {title[-4:]} is 85000 [1].",
                    "Tuition is 85000 per year.",
                    titles=(title,),
                )
                self.accepted(
                    f"The tuition for {title[-9:-5]} is 85000 [1].",
                    "Tuition is 85000 per year.",
                    titles=(title,),
                )
        self.rejected(
            "The tuition for 2022 is 85000 [1].",
            "Tuition is 85000 per year.",
            titles=("Batch 2020" + EN + "2024",),
        )

    def test_an_interval_is_not_a_citation(self):
        passage = (
            "The coefficient of friction lies between 0 and 1 for the braking surfaces."
        )
        report = self.accepted(
            "The coefficient of friction lies in the interval [0, 1] for the "
            "braking surfaces [1].",
            passage,
        )

        self.assertEqual(report.cited_markers, (1,))
        self.assertEqual(report.invalid_markers, ())
        self.assertIn("[0, 1]", report.text)

    def test_a_group_of_real_markers_is_still_split(self):
        report = self.accepted(
            "Tuition is Rs 85,000 and the exam fee Rs 4,500 [1, 2].",
            "Tuition is Rs 85,000.",
            "The exam fee is Rs 4,500.",
        )

        self.assertEqual(report.cited_markers, (1, 2))
        self.assertIn("[1][2]", report.text)

    def test_a_group_with_a_number_that_is_no_passage_is_left_as_written(self):
        passages = ("Tuition is Rs 85,000 per year.", "The exam fee is Rs 4,500.")
        for group in ("[1, 3]", "[2, 12]", "[0, 1]"):
            with self.subTest(group=group):
                report = check(f"Tuition is Rs 85,000 per year {group}.", *passages)
                self.assertFalse(report.ok)
                self.assertIn(group, report.text)

    def test_a_group_that_names_a_missing_passage_cannot_hide_behind_a_real_one(self):
        """The cheapest hallucination signal is still a signal: the 3 is a
        figure the passages do not state."""
        report = check(
            "Tuition is Rs 85,000 per year [1] [1, 3].",
            "Tuition is Rs 85,000 per year.",
            "The exam fee is Rs 4,500.",
        )

        self.assertFalse(report.ok)
        self.assertIn("3", report.unsupported_facts)


# --- grounding/F5(1) and engine/F2: the user's own figure ---------------------

ATTENDANCE = (
    "A student with attendance below 75 percent is not eligible for the exams, "
    "but the Dean may condone a shortfall down to 65 percent on medical grounds."
)
SCHOLARSHIP_AID = (
    "The merit scholarship is open to students whose annual family income is "
    "below Rs 3 lakh per year."
)


class UserFigureTests(Base):
    def test_a_figure_the_user_typed_may_be_echoed(self):
        answer = (
            "At 60 percent attendance you would not be eligible, since the "
            "minimum is 75 percent [1]."
        )

        self.rejected(answer, ATTENDANCE, figure="60")
        self.accepted(
            answer,
            ATTENDANCE,
            user_text="Can a student with 60 percent attendance apply?",
        )

    def test_a_figure_typed_in_words_is_echoed_in_digits_and_back(self):
        self.accepted(
            "With Rs 2 lakh family income you are below the Rs 3 lakh limit, "
            "so you qualify [1].",
            SCHOLARSHIP_AID,
            user_text="My family earns two lakh rupees. Do I qualify?",
        )
        self.accepted(
            "With a family income of two lakh rupees you are below Rs 3 lakh [1].",
            SCHOLARSHIP_AID,
            user_text="My family income is Rs 200000. Do I qualify?",
        )

    def test_a_date_the_user_gave_about_themselves_may_be_echoed(self):
        self.accepted(
            "You joined on 15 August, and the first semester fee is due on 31 March [1].",
            "The first semester fee is due on 31 March every year.",
            user_text="I joined on 15 August 2025. When is my fee due?",
        )

    def test_a_date_the_user_only_heard_is_not_a_given(self):
        self.rejected(
            "The hostel closes on 31 March, not on 15 August [1].",
            "The hostel closes on 31 March every year for the summer vacation.",
            user_text="I heard the hostel closes on 15 August 2025?",
        )

    def test_it_is_not_a_licence_for_any_other_figure(self):
        self.rejected(
            "At 70 percent attendance you would be eligible, since the minimum "
            "is 75 percent [1].",
            ATTENDANCE,
            figure="70",
            user_text="Can a student with 60 percent attendance apply?",
        )

    def test_it_never_stands_for_a_citation(self):
        report = check(
            "At 60 percent attendance you would not be eligible.",
            ATTENDANCE,
            user_text="Can a student with 60 percent attendance apply?",
        )

        self.assertFalse(report.ok)
        self.assertTrue(any("cites no passage" in f for f in report.failures))

    def test_it_is_never_used_to_repair_a_citation(self):
        """The user gave 42000 as their own figure; a figure is attributed to a
        passage only by the passage's own text, and a user's figure needs no
        attribution."""
        context = build(
            "Tuition is 85000 per year for the programme.",
            "The hostel fee is 42000 per year for residents.",
        )
        report = grounding.validate(
            "Tuition is 85000 [1] and the hostel fee is 42000.",
            context,
            user_text="I paid 42000 for the hostel last year.",
        )

        self.assertTrue(report.ok)
        repaired, repairs = grounding.repair_citations(report, context)
        self.assertEqual(repairs, [])
        self.assertEqual(repaired, report.text)

    def test_no_user_text_changes_nothing(self):
        report = check(
            "At 60 percent attendance you would not be eligible [1].", ATTENDANCE
        )
        self.assertFalse(report.ok)
        self.assertIn("60", report.unsupported_facts)


class AFigureTheUserAskedAboutIsNotAGivenTests(Base):
    """Echoing back a number the user is asking about is how an answer agrees
    with a wrong number, or answers for a year the documents do not cover. Only
    what the user gave as their OWN situation may be repeated."""

    BUS_PASS = (
        "A bus pass costs 4500 rupees per semester for the academic year 2024-25."
    )

    def test_a_claim_in_the_question_is_not_a_given(self):
        for question in (
            "Is the minimum attendance 70 percent?",
            "Do I need 70 percent attendance?",
            "I heard the minimum is 70 percent, is that right?",
            "What is the fine if the limit is 70 percent?",
            # No word that asks for a requirement, and nobody's situation: a
            # figure that is not clearly the user's own is not a given.
            "Is attendance of 70 percent enough?",
        ):
            with self.subTest(question=question):
                self.rejected(
                    "The minimum attendance is 70 percent [1].",
                    ATTENDANCE,
                    figure="70",
                    user_text=question,
                )

    def test_a_year_the_user_asked_about_is_not_a_given(self):
        for question in (
            "What does a bus pass cost for the 2023-24 year?",
            "How much is the bus pass in 2023-24?",
        ):
            with self.subTest(question=question):
                self.rejected(
                    "The 2023-24 bus pass costs 4500 rupees per semester [1].",
                    self.BUS_PASS,
                    user_text=question,
                )

    def test_their_own_situation_in_another_sentence_is_still_a_given(self):
        self.accepted(
            "At 70 percent attendance you are below the minimum of 75 percent [1].",
            ATTENDANCE,
            user_text="I have 70 percent attendance. What is the minimum for exams?",
        )

    def test_a_third_person_situation_is_a_given(self):
        self.accepted(
            "A student with 60 percent attendance is below the minimum of 75 "
            "percent [1].",
            ATTENDANCE,
            user_text="Can a student with 60 percent attendance apply for the exam?",
        )

    def test_what_counts_as_their_own_situation(self):
        kept = grounding.user_stated_text
        self.assertEqual(kept("Is the minimum attendance 70 percent?"), "")
        self.assertEqual(kept("What is the tuition fee for 2023-24?"), "")
        self.assertEqual(kept("Do I need 70 percent attendance?"), "")
        self.assertEqual(
            kept("I have 70 percent attendance. What is the minimum?"),
            "I have 70 percent attendance.",
        )
        self.assertEqual(
            kept("My family earns two lakh rupees a year.\nWhat help is there?"),
            "My family earns two lakh rupees a year.",
        )
        self.assertEqual(kept(""), "")
        self.assertEqual(kept(None), "")


# --- grounding/F5(2): list numbering and emphasis ------------------------------

FEES = "Tuition is Rs 85,000. Exam fee is Rs 4,500. Hostel fee is Rs 58,000."
ITEMS = (("Tuition", "Rs 85,000"), ("Exam fee", "Rs 4,500"), ("Hostel", "Rs 58,000"))
LIST_STYLES = {
    "plain": lambda n, a, b: f"{n}. {a}: {b} [1]",
    "paren": lambda n, a, b: f"{n}) {a}: {b} [1]",
    "bold number": lambda n, a, b: f"**{n}.** {a}: {b} [1]",
    "bold line": lambda n, a, b: f"**{n}. {a}:** {b} [1]",
    "bullet bold": lambda n, a, b: f"* **{n}.** {a}: {b} [1]",
    "bracketed": lambda n, a, b: f"({n}) {a}: {b} [1]",
    "colon": lambda n, a, b: f"{n}: {a}: {b} [1]",
    "heading": lambda n, a, b: f"### {n}. {a}" + NEWLINE + f"{b} [1]",
    "quote": lambda n, a, b: f"> {n}. {a}: {b} [1]",
    "escaped": lambda n, a, b: f"{n}" + chr(92) + f". {a}: {b} [1]",
    "indented": lambda n, a, b: f"  {n}. {a}: {b} [1]",
    "bold emphasis": lambda n, a, b: f"- **{a}:** {b} [1]",
}


class ListNumberingTests(Base):
    def listing(self, style, first):
        make = LIST_STYLES[style]
        lines = [make(first + i, a, b) for i, (a, b) in enumerate(ITEMS)]
        return "For B.Tech CSE:" + NEWLINE + NEWLINE.join(lines)

    def test_a_numbered_line_is_formatting_in_every_common_style(self):
        for style in LIST_STYLES:
            for first in (1, 4):
                with self.subTest(style=style, first=first):
                    self.accepted(self.listing(style, first), FEES)

    def test_a_wrong_figure_in_a_list_is_still_caught(self):
        for style in LIST_STYLES:
            with self.subTest(style=style):
                answer = self.listing(style, 1).replace("Rs 4,500", "Rs 4,800")
                self.rejected(answer, FEES, figure="4800")

    def test_a_figure_that_opens_a_line_is_still_a_figure(self):
        for line in (
            "80 percent attendance is the minimum [1].",
            "**80** percent attendance is the minimum [1].",
            "80 percent: that is the minimum [1].",
        ):
            with self.subTest(line=line):
                self.rejected(line, ATTENDANCE, figure="80")

    def test_emphasis_around_a_figure_is_not_part_of_it(self):
        self.accepted(
            "You need a minimum of **75 percent** attendance, or __65 percent__ with "
            "condonation [1].",
            ATTENDANCE,
        )


# --- grounding/F5(3): the ways a model writes a citation ----------------------

TWO = ("Tuition is Rs 85,000 per year.", "The exam fee is Rs 4,500 per year.")
BOTH = "Tuition is Rs 85,000 and the exam fee is Rs 4,500"


class CitationFormTests(Base):
    def test_these_are_citations_of_both_passages(self):
        for form in (
            "[1-2]",
            "[1" + EN + "2]",
            "[1" + EM + "2]",
            "[ 1 ] [ 2 ]",
            "[Source 1, Source 2]",
            "[Sources 1-2]",
            "[1 and 2]",
            "[1 & 2]",
            "[1, 2]",
            "[1; 2]",
            "[1][2]",
            "\N{LEFT BLACK LENTICULAR BRACKET}1\N{RIGHT BLACK LENTICULAR BRACKET}"
            "\N{LEFT BLACK LENTICULAR BRACKET}2\N{RIGHT BLACK LENTICULAR BRACKET}",
            "\N{FULLWIDTH LEFT SQUARE BRACKET}1-2\N{FULLWIDTH RIGHT SQUARE BRACKET}",
            "[^1][^2]",
        ):
            with self.subTest(form=form):
                report = self.accepted(f"{BOTH} {form}.", *TWO)
                self.assertEqual(report.cited_markers, (1, 2))
                self.assertEqual(re.findall(r"\[(\d+)\]", report.text), ["1", "2"])
                self.assertEqual(
                    [
                        c["marker"]
                        for c in grounding.citations_for(report.text, build(*TWO))
                    ],
                    [1, 2],
                )

    def test_one_passage_in_a_longer_form(self):
        for form in ("[ 1 ]", "[Source 1]", "[Passage 1]"):
            with self.subTest(form=form):
                report = self.accepted(f"Tuition is Rs 85,000 per year {form}.", *TWO)
                self.assertEqual(report.cited_markers, (1,))

    def test_a_number_in_parentheses_is_not_a_citation(self):
        report = check("Tuition is Rs 85,000 per year (1).", *TWO)

        self.assertFalse(report.ok)
        self.assertTrue(any("cites no passage" in f for f in report.failures))

    def test_a_form_that_names_a_missing_passage_still_fails(self):
        for form in ("[ 7 ]", "[Source 7]"):
            with self.subTest(form=form):
                report = check(f"Tuition is Rs 85,000 per year {form}.", *TWO)
                self.assertFalse(report.ok)
                self.assertEqual(report.invalid_markers, (7,))

    def test_a_range_that_runs_past_the_last_passage_is_left_alone(self):
        for form in ("[1-3]", "[0-1]", "[2-4]"):
            with self.subTest(form=form):
                report = check(f"Tuition is Rs 85,000 per year {form}.", *TWO)
                self.assertFalse(report.ok)
                self.assertIn(form, report.text)

    def test_the_forms_split_without_a_context_as_they_always_did(self):
        self.assertEqual(
            facts.normalize_markers("Yes [1, 2]; also [3;4]."),
            "Yes [1][2]; also [3][4].",
        )
        self.assertEqual(facts.normalize_markers("Yes [1-3]."), "Yes [1][2][3].")
        self.assertEqual(
            facts.normalize_markers("Yes [1] and [2]."), "Yes [1] and [2]."
        )
        self.assertEqual(
            facts.normalize_markers("Yes [1, 2] [0, 1].", available={1, 2}),
            "Yes [1][2] [0, 1].",
        )

    def test_extract_markers_reads_the_forms(self):
        self.assertEqual(
            grounding.extract_markers("a [Source 2] b [1-2] c [ 3 ]"), (2, 1, 3)
        )
        self.assertEqual(
            grounding.extract_markers("a [0, 1] b [1]", available={1}), (1,)
        )


# --- grounding/F5(4): "15-day" is "15 days" -----------------------------------


class GluedCompoundTests(Base):
    def test_a_compound_is_its_number(self):
        for passage, answer in (
            (
                "Revaluation applications must be filed within 15 days of the result.",
                "There is a 15-day window to file a revaluation application [1].",
            ),
            (
                "A grievance must be submitted within 30 days of the incident.",
                "A 30-day limit applies to a grievance from the incident [1].",
            ),
            (
                "A summer internship of at least 8 weeks may be credited.",
                "An 8-week summer internship may be credited [1].",
            ),
            (
                "A password must be at least 12 characters long.",
                "Your password must be a 12-character minimum [1].",
            ),
            (
                "The late payment surcharge is 500 rupees per week.",
                "The late payment surcharge is Rs 500/week [1].",
            ),
            (
                "Tuition is 85000 per year for the programme.",
                "Tuition is Rs 85,000/year for the programme [1].",
            ),
            (
                "Working Saturdays are the 2nd and 3rd Saturday of the month.",
                "Working Saturdays are the 2nd/3rd Saturday of the month [1].",
            ),
        ):
            with self.subTest(answer=answer):
                self.accepted(answer, passage)

    def test_a_compound_in_the_passage_supports_the_plain_figure(self):
        self.accepted(
            "Revaluation applications must be filed within 15 days [1].",
            "Revaluation applications must be filed in a 15-day window after the result.",
        )

    def test_a_different_number_in_a_compound_is_still_caught(self):
        for answer, passage, figure in (
            (
                "There is a 16-day window to file a revaluation application [1].",
                "Revaluation applications must be filed within 15 days of the result.",
                "16",
            ),
            (
                "The late payment surcharge is Rs 600/week [1].",
                "The late payment surcharge is 500 rupees per week.",
                "600",
            ),
            (
                "Tuition is Rs 86,000/year for the programme [1].",
                "Tuition is 85000 per year for the programme.",
                "86000",
            ),
            (
                "Working Saturdays are the 2nd/4th Saturday of the month [1].",
                "Working Saturdays are the 2nd and 3rd Saturday of the month.",
                "4th",
            ),
        ):
            with self.subTest(answer=answer):
                self.rejected(answer, passage, figure=figure)

    def test_an_identifier_is_not_taken_for_a_compound(self):
        self.rejected(
            "See circular REG/2024/118 [1].",
            "See circular REG/2024/117 for the schedule.",
        )

    def test_a_day_and_month_written_with_a_hyphen_is_not_a_number_and_a_unit(self):
        """ "15-Sep" would be read as the figure 15, which any 15 in the passage
        supports: the month it names is part of what it says."""
        self.rejected(
            "Registration closes on 15-Sep [1].",
            "Registration closes on 15 August 2025 for the placement cell.",
        )


# --- grounding/F5(5): clock times ----------------------------------------------

BUS = "On weekdays the last bus leaves the campus at 7.30 pm and the first at 7 am."
HOURS = "Working hours are 9 am to 5 pm on weekdays."


class ClockTimeTests(Base):
    def test_the_same_time_written_two_ways(self):
        for answer, passage in (
            ("The last bus leaves at 7:30 PM [1].", BUS),
            ("The last bus leaves at 7.30 PM [1].", BUS),
            ("The last bus leaves at 7:30pm [1].", BUS),
            ("The last bus leaves at 7:30 p.m. [1].", BUS),
            ("The first bus leaves at 7:00 AM [1].", BUS),
            ("The first bus leaves at 07:00 [1].", BUS),
            ("The last bus leaves at 19:30 [1].", BUS),
            ("Working hours are 09:00 to 17:00 [1].", HOURS),
            ("Working hours are 9:00 AM to 5:00 PM [1].", HOURS),
            ("Working hours are 9 AM-5 PM [1].", HOURS),
            ("Working hours are 17:00 until 9 am [1].", HOURS),
        ):
            with self.subTest(answer=answer):
                self.accepted(answer, passage)

    def test_a_passage_in_24_hour_time_supports_the_12_hour_answer(self):
        self.accepted(
            "The last bus leaves at 7:30 PM [1].",
            "On weekdays the last bus leaves the campus at 19:30.",
        )
        self.accepted(
            "The office opens at 9 AM [1].",
            "The office is open from 09:00 on weekdays.",
        )

    def test_a_different_time_is_still_caught(self):
        for answer, figure in (
            ("The last bus leaves at 7:45 PM [1].", None),
            ("The last bus leaves at 7:30 AM [1].", None),
            ("The last bus leaves at 18:30 [1].", None),
            ("The last bus leaves at 8.30 pm [1].", None),
            ("Working hours are 09:00 to 18:00 [1].", None),
        ):
            with self.subTest(answer=answer):
                text = HOURS if "Working" in answer else BUS
                self.rejected(answer, text, figure=figure)

    def test_nine_in_the_morning_is_not_nine_at_night(self):
        self.rejected(
            "The library opens at 09:00 [1].",
            "The library opens at 9 pm during the examination weeks.",
        )

    def test_a_12_hour_hour_with_no_meridiem_is_not_read_as_24_hour(self):
        self.assertEqual(facts.extract_times("at 5:00 and 7:30")[1], [])
        self.assertEqual(
            sorted(facts.extract_times("at 17:00 and 09:30 and 7:30 pm")[1]),
            [
                ("time:09:30", "09:30"),
                ("time:17:00", "17:00"),
                ("time:19:30", "7:30"),
            ],
        )

    def test_an_hour_that_is_no_time_is_left_alone(self):
        for text in ("13 pm", "5 amendments", "7:75 pm", "24:00 and 12:75"):
            with self.subTest(text=text):
                self.assertEqual(facts.extract_times(text)[1], [])


# --- grounding/F5(6): lakh and crore --------------------------------------------


class LakhAndCroreTests(Base):
    def test_a_lakh_is_a_hundred_thousand_in_both_directions(self):
        for answer, passage in (
            (
                "Income must be below Rs 3 lakh [1].",
                "Income must be below Rs 300000 per year.",
            ),
            (
                "Income must be below Rs 300000 [1].",
                "Income must be below Rs 3 lakh per year.",
            ),
            (
                "Income must be below Rs 3,00,000 [1].",
                "Income must be below Rs 3 lakh per year.",
            ),
            (
                "Income must be below Rs 3 lakhs [1].",
                "Income must be below Rs 3,00,000 per year.",
            ),
            (
                "Income must be below Rs 2.5 lakh [1].",
                "Income must be below Rs 250000 per year.",
            ),
            (
                "Income must be below Rs 250000 [1].",
                "Income must be below Rs 2.5 lakh per year.",
            ),
            (
                "The grant is Rs 1 crore [1].",
                "The grant is Rs 10000000 for the project.",
            ),
            (
                "The grant is Rs 10000000 [1].",
                "The grant is Rs 1 crore for the project.",
            ),
            (
                "The grant is Rs 1,00,00,000 [1].",
                "The grant is Rs 1 crore for the project.",
            ),
            (
                "Income must be below Rs 3 lakh [1].",
                "Income must be below three lakh rupees per year.",
            ),
        ):
            with self.subTest(answer=answer):
                self.accepted(answer, passage)

    def test_a_different_amount_is_still_caught(self):
        for answer, passage in (
            (
                "Income must be below Rs 4 lakh [1].",
                "Income must be below Rs 300000 per year.",
            ),
            (
                "Income must be below Rs 3.5 lakh [1].",
                "Income must be below Rs 300000 per year.",
            ),
            (
                "Income must be below Rs 2.5 lakh [1].",
                "Income must be below Rs 2,00,000 per year.",
            ),
            (
                "Income must be below Rs 1 lakh [1].",
                "Income must be below Rs 10000 per year.",
            ),
            (
                "The grant is Rs 2 crore [1].",
                "The grant is Rs 1 crore for the project.",
            ),
            ("The grant is Rs 1 crore [1].", "The grant is Rs 1 lakh for the project."),
        ):
            with self.subTest(answer=answer):
                self.rejected(answer, passage)

    def test_the_amount_is_exact(self):
        self.assertEqual(
            facts.amounts("Rs 2.5 lakh and 1 crore"), "Rs 250000 and 10000000"
        )
        self.assertEqual(facts.amounts("0.85 lakh"), "85000")
        self.assertEqual(facts.amounts("3 lakhs, 3 lacs"), "300000, 300000")
        self.assertEqual(facts.amounts("a lakh of people"), "a lakh of people")


# --- grounding/F3: invisible characters and commas -------------------------------


class QuietCharacterTests(Base):
    def test_an_invisible_character_does_not_split_a_figure(self):
        passage = "The pass mark is 7 and the laboratory mark is 5."
        for invisible in (chr(0x200B), chr(0xAD), chr(0x200D), chr(0xFEFF)):
            with self.subTest(code=hex(ord(invisible))):
                self.rejected(
                    f"The pass mark is 7{invisible}5 [1].", passage, figure="75"
                )

    def test_an_invisible_character_does_not_hide_a_figure_in_the_passage(self):
        for invisible in (chr(0x200B), chr(0xAD)):
            with self.subTest(code=hex(ord(invisible))):
                self.accepted(
                    "Revaluation applications are filed within 15 days [1].",
                    f"Revaluation applications are filed within 1{invisible}5 days of the result.",
                )

    def test_a_comma_is_a_thousands_separator_only_in_well_formed_groups(self):
        self.rejected(
            "Attendance of 7,5 percent is required [1].",
            "Attendance of 75 percent is required.",
        )
        self.rejected(
            "Semesters 1,2,3 are covered [1].",
            "Semesters 123 are covered by the rules.",
        )
        self.rejected(
            "The fee is Rs 12,34 [1].", "The fee is Rs 1234 per year for the course."
        )

    def test_well_formed_groups_are_still_one_figure(self):
        for written, plain in (
            ("85,000", "85000"),
            ("8,50,000", "850000"),
            ("1,23,45,678", "12345678"),
            ("1,234,567", "1234567"),
        ):
            with self.subTest(written=written):
                self.accepted(
                    f"The fee is Rs {written} [1].",
                    f"The fee is Rs {plain} per year for the course.",
                )

    def test_separate_numbers_in_a_list_stay_separate(self):
        self.accepted(
            "Semesters 1,2,3 are covered [1].",
            "Semesters 1, 2 and 3 are covered by the rules.",
        )
        self.assertEqual(
            facts.digit_commas("1,2,3 and 85,000 and 7,5"), "1 2 3 and 85000 and 7 5"
        )


# --- grounding/F6: a long run must not cost seconds -----------------------------


class LongRunTests(Base):
    PASSAGE = "Attendance of 75 percent is the minimum required in every course."

    def timed(self, answer, *texts):
        started = time.perf_counter()
        check(answer, *(texts or (self.PASSAGE,)))
        return time.perf_counter() - started

    def test_a_long_run_of_letters(self):
        started = time.perf_counter()
        grounding.FACT.findall("A" * 20000)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertLess(
            self.timed("The minimum is 75 percent [1]. " + "A" * 20000), 3.0
        )

    def test_a_long_run_of_newlines(self):
        self.assertLess(
            self.timed("The minimum is 75 percent [1]." + NEWLINE * 20000), 3.0
        )

    def test_a_long_run_of_blank_lines_made_of_spaces(self):
        self.assertLess(
            self.timed("The minimum is 75 percent [1]." + (" " + NEWLINE) * 8000), 3.0
        )

    def test_a_long_run_of_letters_in_a_passage(self):
        self.assertLess(
            self.timed(
                "The minimum is 75 percent [1].", self.PASSAGE + " " + "z" * 20000
            ),
            3.0,
        )

    def test_a_long_run_of_digits_and_commas(self):
        self.assertLess(
            self.timed("The minimum is 75 percent [1]. " + "1" * 20000), 3.0
        )
        self.assertLess(
            self.timed("The minimum is 75 percent [1]. " + "1," * 10000), 3.0
        )

    def test_a_figure_followed_by_a_long_run_of_blanks(self):
        """The amount ("3 lakh") and the dash between digits both look past the
        blanks after a figure."""
        for blank in (" ", "\t", " -"):
            with self.subTest(blank=repr(blank)):
                self.assertLess(
                    self.timed("The minimum is 75 percent [1]. 1" + blank * 20000), 3.0
                )

    def test_the_figures_of_a_letter_prefix_are_still_read(self):
        self.assertEqual(
            grounding._facts("Rs1200 and rupees500 and abcdefghijkl12"),
            {"rs1200", "rupees500", "abcdefghijkl12"},
        )

    def test_a_blank_line_is_still_a_line_break_and_a_number_still_a_number(self):
        report = check(
            "1. Attendance is 75 percent [1]."
            + NEWLINE * 5
            + "2. Condonation needs 65 percent [1].",
            "Attendance is 75 percent. Condonation needs 65 percent.",
        )
        self.assertTrue(report.ok, report.failures)


# --- deploy/F6(a): a number too large to total must not withhold the answer -------


class HugeCellTests(SimpleTestCase):
    def table(self, serial):
        rows = f"Alice | 1000 | {serial}{NEWLINE}Bob | 2000 | {serial}{NEWLINE}Cara | 3000 | 7"
        return make_chunk(1, rows, header=("Name", "Pay", "Serial"))

    def test_a_27_digit_cell_skips_that_aggregate_and_not_the_answer(self):
        for digits in (26, 27, 30, 60):
            serial = "9" * digits
            with self.subTest(digits=digits):
                context = ctx.BuiltContext(
                    passages=[
                        ctx.Passage(
                            marker=1,
                            chunk=self.table(serial),
                            text=self.table(serial).text,
                            relevance=1.0,
                            table_header=("Name", "Pay", "Serial"),
                        )
                    ]
                )
                report = grounding.validate(
                    "The total pay is 6000 for 3 rows [1].", context
                )
                self.assertTrue(report.ok, report.failures)
                self.assertIn("6000", tables.derived_figures(context.passages))

    def test_a_cell_of_26_digits_is_still_summed(self):
        chunk_ = self.table("1" * 26)
        figures = tables.derived_figures(
            [ctx.Passage(marker=1, chunk=chunk_, text=chunk_.text, relevance=1.0)]
        )
        self.assertIn("1" * 26, figures)

    def test_the_forms_of_a_number_that_cannot_be_written_are_none(self):
        self.assertEqual(tables._forms(tables.Decimal("9" * 40)), set())
        self.assertIn("12", tables._forms(tables.Decimal("12")))


# --- deploy/F6(b): what a model and a reader are told is readable -----------------


class ReadableFailureTests(Base):
    def test_a_date_is_named_as_a_person_writes_it(self):
        for fact, shown in (
            ("date:--08-15", "15 August"),
            ("date:2025-09-16", "16 September 2025"),
            ("date:?31april2025", "31 April 2025 (not a real date)"),
            ("date:?31stapril2025", "31 April 2025 (not a real date)"),
            ("date:?april312025", "31 April 2025 (not a real date)"),
            ("date:?30february", "30 February (not a real date)"),
            ("date:?25to35march2025", "25 to 35 March 2025 (not a real date)"),
            ("85000", "85000"),
            ("2024-2025", "2024-2025"),
            ("time:19:30|7:30", "7:30"),
        ):
            with self.subTest(fact=fact):
                self.assertEqual(facts.readable(fact), shown)

    def test_the_correction_says_what_a_person_would(self):
        for answer, shown in (
            ("The deadline is 15 August [1].", "15 August"),
            ("The deadline is 31 April 2025 [1].", "31 April 2025 (not a real date)"),
            ("The deadline is 16 September 2025 [1].", "16 September 2025"),
        ):
            with self.subTest(answer=answer):
                report = check(
                    answer, "The deadline for the form is 31 March 2025 for all."
                )
                correction = grounding.correction_for(report)
                self.assertIn(shown, correction)
                self.assertNotIn("date:", correction)
                self.assertNotIn("--08", correction)
                self.assertNotIn("?31april", correction)

    def test_the_failure_a_reader_or_an_api_sees_is_readable(self):
        report = check(
            "The deadline is 15 August [1].",
            "The deadline for the form is 31 March 2025 for all.",
        )

        shown = " ".join(report.failures)
        self.assertIn("15 August", shown)
        self.assertNotIn("date:", shown)
        self.assertEqual(report.unsupported_facts, ("date:--08-15",))

    def test_the_orchestrator_can_name_a_repaired_figure_the_same_way(self):
        self.assertEqual(grounding.readable("date:2025-03-31"), "31 March 2025")
        self.assertEqual(grounding.readable("200"), "200")


class AClaimWithAPronounIsNotASituationTests(Base):
    """Round two found that a pronoun and no ask-word was enough to make a
    CLAIM count as the user's own situation: 'Can I borrow 10 books?' let an
    answer agree that 10 books may be borrowed. A clause counts only when it
    STATES a situation, and not when it asks, hedges or reports hearsay."""

    LIBRARY = "An undergraduate student may borrow up to four books at one time."
    ATTENDANCE_RULE = "A student must have a minimum of 75 percent attendance."

    def test_questions_and_hearsay_with_a_pronoun_are_not_given(self):
        for question in (
            "Is 70 percent enough for me?",
            "Can I get a 20 percent scholarship?",
            "We heard the hostel closes on 15 August",
            "My professor says 70 is the cut-off",
            "I read that attendance is 70 percent, true?",
            "Our department says the bus pass is 3000 rupees, right?",
            "Am I allowed 70 percent leave?",
        ):
            with self.subTest(question=question):
                self.assertEqual(facts.user_stated_text(question), "")

    def test_an_answer_that_agrees_with_a_figure_in_such_a_question_is_rejected(self):
        self.rejected(
            "A student may borrow 10 books [1].",
            self.LIBRARY,
            figure="10",
            user_text="Can I borrow 10 books?",
        )
        self.rejected(
            "70 percent attendance is enough to sit the examination [1].",
            self.ATTENDANCE_RULE,
            figure="70",
            user_text="Is 70 percent enough for me?",
        )

    def test_a_genuine_situation_survives_a_question_in_the_same_sentence(self):
        """The first half of 'I have 70 percent attendance, what is the minimum
        required?' is the situation; the ask-word was in the other half."""
        for question in (
            "I have 70 percent attendance, what is the minimum required?",
            "I have 70 percent attendance, is that fine for the exam?",
            "Our family income is Rs 2 lakh, what is the amount of aid I can get?",
            "I joined in 2022 and my hostel fee was 55000. What is it now?",
            "I was unwell and my presence is at 70 percent. Can I still sit the exam?",
        ):
            with self.subTest(question=question):
                self.assertNotEqual(facts.user_stated_text(question), "")

        self.accepted(
            "At 70 percent you are below the 75 percent minimum [1].",
            self.ATTENDANCE_RULE,
            user_text="I have 70 percent attendance, what is the minimum required?",
        )

    def test_a_roman_numeral_or_an_abbreviation_is_not_a_pronoun(self):
        for text in (
            "clause (i) of 4.2 and 70 percent applies",
            "Attendance, i.e. 70 percent, is the target",
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.user_stated_text(text), "")

    def test_a_follow_up_cannot_join_a_pronoun_to_another_turns_figure(self):
        """The engine hands over what the user typed THIS turn, not the retrieval
        query (their words with terms borrowed from earlier turns appended, with
        no sentence break). 'what about me?' beside borrowed 'attendance 70
        percent enough' used to read as one sentence about the user."""
        self.assertEqual(facts.user_stated_text("what about me?"), "")
        self.rejected(
            "The minimum is 70 percent [1].",
            self.ATTENDANCE_RULE,
            figure="70",
            user_text="what about me?",
        )


class IdentifiersWithALetterSuffixStayWholeTests(Base):
    """'16-A', '12/B' and '80-C' were read as the bare numbers 16, 12 and 80, so
    a wrong form, section or room passed for the right one, and a passage's
    'Section 80-C' supported '80 percent'. Only a unit word glues to a number."""

    def test_a_different_suffix_is_a_different_identifier(self):
        for answer, passage in (
            ("Submit Form 16-B [1].", "Submit Form 16-A to the office."),
            ("Section 12-B applies [1].", "Section 12-A applies to staff."),
            ("See Rule 4/b [1].", "See Rule 4/a for the details."),
            ("The exam is in Hall 3-B [1].", "The exam is held in Hall 3-A."),
            ("Section 80-CCD applies [1].", "Section 80-C applies to every employee."),
        ):
            with self.subTest(answer=answer):
                self.rejected(answer, passage)

    def test_the_same_identifier_is_accepted(self):
        self.accepted("Submit Form 16-A [1].", "Submit Form 16-A to the office.")

    def test_an_identifier_never_stands_for_a_bare_number_either_way(self):
        self.rejected(
            "Submit Form 12-A [1].", "The result is published within 12 days."
        )
        self.rejected(
            "Section 80-C applies [1].", "Attendance of 80 percent is required."
        )
        self.rejected(
            "A student needs 80 percent attendance [1].",
            "As set out in Section 80-C of the rules.",
        )

    def test_a_unit_word_still_glues_to_its_number(self):
        for compound in (
            "15-day",
            "8-week",
            "12-character",
            "30-days",
            "15-day-window",
        ):
            with self.subTest(compound=compound):
                self.assertEqual(
                    grounding._split_glued(compound), [compound.split("-")[0]]
                )
        self.assertEqual(grounding._split_glued("rs500/week"), ["500"])
        # ...but a month is not a unit: "15-Sep" is a date.
        self.assertEqual(grounding._split_glued("15-sep"), ["15-sep"])


# --- found by running the benchmark through a real model ---------------------

NBSP = chr(0xA0)
NNBSP = chr(0x202F)
THIN_SPACE = chr(0x2009)
FIGURE_SPACE = chr(0x2007)
HYPHEN_LIKES = {
    "hyphen U+2010": chr(0x2010),
    "non-breaking hyphen U+2011": chr(0x2011),
    "figure dash U+2012": chr(0x2012),
    "minus sign U+2212": chr(0x2212),
    "small hyphen-minus U+FE63": chr(0xFE63),
    "fullwidth hyphen-minus U+FF0D": chr(0xFF0D),
}
SPACES = (
    ("no-break space", NBSP),
    ("narrow no-break space", NNBSP),
    ("thin space", THIN_SPACE),
    ("figure space", FIGURE_SPACE),
)
REGISTER = (
    "A student must register with the placement cell before 15 August of the "
    "final year."
)
CURFEW = "The hostel curfew is 10 pm on weekdays and 11 pm on Saturdays."
YEAR_FEES = (
    "Fee structure for the year 2024-25 is given below. B.Tech CSE tuition is "
    "85000 per year."
)
CONDONATION = (
    "A student with attendance between 65-75 percent may apply for condonation."
)
FINE = "A library book returned late attracts a fine of 5 rupees per day."


class LookalikeCharacterTests(Base):
    """gpt-oss, the second link of the live chain, writes a NARROW NO-BREAK SPACE
    between a number and its unit or month in most of its answers and a
    NON-BREAKING HYPHEN in "2024-25". Every pattern looked for an ordinary space
    and hyphen: a date whose space was not read was only its day number, which "15
    August" supports ("15 September" was ACCEPTED, G1), and a range whose hyphen
    was not read was two figures nobody wrote (a correct answer was refused, and a
    Word or PDF passage with a no-break space in its date was refused the same
    way)."""

    def test_a_date_with_a_lookalike_space_is_still_a_date(self):
        for name, space in SPACES:
            with self.subTest(space=name):
                self.accepted(f"Register before 15{space}August [1].", REGISTER)
                self.rejected(f"Register before 15{space}September [1].", REGISTER)
                self.rejected(f"Register before 16{space}August [1].", REGISTER)

    def test_a_passage_with_a_lookalike_space_in_its_date_still_supports_it(self):
        for name, space in SPACES:
            passage = REGISTER.replace("15 August", f"15{space}August")
            with self.subTest(space=name):
                self.accepted("Register before 15 August [1].", passage)
                self.rejected("Register before 15 September [1].", passage)

    def test_a_time_an_amount_and_a_percentage_with_a_lookalike_space(self):
        self.accepted(f"The curfew on Saturdays is 11{NNBSP}pm [1].", CURFEW)
        self.accepted(f"The curfew on Saturdays is 11.00{NNBSP}p.m. [1].", CURFEW)
        self.rejected(f"The curfew on Saturdays is 9{NNBSP}pm [1].", CURFEW)
        self.accepted(f"The fine is 5{NNBSP}rupees per day [1].", FINE)
        self.rejected(f"The fine is 50{NNBSP}rupees per day [1].", FINE)
        self.accepted(f"You need 75{NNBSP}percent [1].", "A minimum of 75 percent.")
        self.rejected(f"You need 70{NNBSP}percent [1].", "A minimum of 75 percent.")

    def test_a_range_written_with_a_lookalike_hyphen(self):
        for name, dash in HYPHEN_LIKES.items():
            with self.subTest(dash=name):
                self.accepted(f"The tuition for 2024{dash}25 is 85000 [1].", YEAR_FEES)
                self.rejected(f"The tuition for 2023{dash}24 is 85000 [1].", YEAR_FEES)
                self.accepted(f"Between 65{dash}75 percent [1].", CONDONATION)
                self.rejected(f"Between 60{dash}75 percent [1].", CONDONATION)

    def test_a_digit_of_another_script_is_that_digit(self):
        passage = "B.Tech CSE tuition is 85000 per year."
        for name, zero in (
            ("fullwidth", 0xFF10),
            ("arabic-indic", 0x660),
            ("devanagari", 0x966),
        ):
            right = "".join(chr(zero + int(digit)) for digit in "85000")
            wrong = "".join(chr(zero + int(digit)) for digit in "95000")
            with self.subTest(script=name):
                self.accepted(f"Tuition is {right} per year [1].", passage)
                self.rejected(f"Tuition is {wrong} per year [1].", passage)
                # ...and a document in that script, answered in ASCII digits.
                written = f"B.Tech CSE tuition is {right} per year."
                self.accepted("Tuition is 85000 per year [1].", written)
                self.rejected("Tuition is 95000 per year [1].", written)

    def test_a_citation_with_a_lookalike_hyphen_or_space(self):
        answer = "Register before 15 August and the curfew is 10 pm [%s]."
        for name, dash in HYPHEN_LIKES.items():
            with self.subTest(dash=name):
                report = self.accepted(answer % f"1{dash}2", REGISTER, CURFEW)
                self.assertEqual(report.cited_markers, (1, 2))
        for inner in (f"1,{NNBSP}2", f"{NBSP}1,{NBSP}2{NNBSP}", f"1{THIN_SPACE}and 2"):
            with self.subTest(inner=inner):
                report = self.accepted(answer % inner, REGISTER, CURFEW)
                self.assertEqual(report.cited_markers, (1, 2))

    def test_what_a_reader_is_shown_is_not_altered(self):
        report = self.accepted(f"Register before 15{NNBSP}August [1].", REGISTER)

        self.assertIn(f"15{NNBSP}August", report.text)

    def test_lookalikes_changes_only_spaces_hyphens_and_decimal_digits(self):
        plain = "Fee Rs 500 - due 15 August 2024 (see [1])"
        self.assertEqual(facts.lookalikes(plain), plain)
        self.assertEqual(
            facts.lookalikes(f"a{NNBSP}b{chr(0x2011)}c{chr(0x668)}"), "a b-c8"
        )
        # Not decimal digits, not spaces, not the hyphens the patterns name.
        for text in (
            "5" + chr(0x2075),  # superscript five
            "65" + EN + "75",  # the range patterns name the en dash
            "65" + EM + "75",
            "a" + chr(0x200B) + "b",  # a format character is strip_format's
        ):
            with self.subTest(text=text):
                self.assertEqual(facts.lookalikes(text), text)

    def test_strip_format_still_strips_only_format_characters(self):
        text = f"a{NNBSP}b{chr(0x2011)}c{chr(0x200B)}"
        self.assertEqual(facts.strip_format(text), f"a{NNBSP}b{chr(0x2011)}c")
