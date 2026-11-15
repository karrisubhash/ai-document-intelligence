"""Fixes for what the third review found in the quoting answerer.

Each of these is a question the base code (79aae20) answered with the right
sentence and the first version of the rewrite refused or answered worse. Every
test that loosens a refusal has a twin that proves the refusal the rewrite was
written for is still made: a programme the fee table does not list, a year the
circular does not cover.
"""

from django.test import SimpleTestCase

from documents.rag import followup, quoting
from documents.rag.llm import ABSTENTION_TEXT, declines_to_answer
from documents.tests_rag_quote import FEE_TABLE, HEADER, ask, prose, table

STAFF_TITLE = "Staff Attendance and Leave Rules"
STAFF_LEAVE = (
    "Each staff member is entitled to 12 days of casual leave in a calendar year."
)
PLACEMENT = "A student must register with the cell before 15 August of the final year."
ATTENDANCE = (
    "A student must maintain a minimum of 75 percent attendance in each course "
    "to be eligible for the end-semester examination."
)


def staff(identifier, text):
    return prose(identifier, text, title=STAFF_TITLE)


class YearlessRuleTests(SimpleTestCase):
    """quote/F1: a year, a join year or a room number in the question."""

    def test_a_year_in_the_question_does_not_refuse_a_year_less_rule(self):
        for question in (
            "How many casual leave days do staff get in 2025?",
            "I joined in 2022. How many days of casual leave do I get?",
            "I am in the 2021 batch. How many days of casual leave do I get?",
        ):
            with self.subTest(question=question):
                response = ask(question, staff(1, STAFF_LEAVE))

                self.assertFalse(response.abstained)
                self.assertIn("12 days of casual leave", response.text)

    def test_a_full_date_whose_day_and_month_the_passage_states_is_answered(self):
        response = ask(
            "Do I need to register before 15 August 2025?", prose(1, PLACEMENT)
        )

        self.assertFalse(response.abstained)
        self.assertIn("15 August", response.text)

    def test_a_room_number_or_a_premise_figure_is_not_a_reason_to_refuse(self):
        for question, passage, expected in (
            (
                "Can visitors come to room 204?",
                "Visitors are permitted in the common room only, between 4 pm "
                "and 7 pm.",
                "common room only",
            ),
            (
                "Is the printing allowance 250 pages per semester?",
                "The printing allowance is 200 pages per semester.",
                "200 pages",
            ),
            (
                "Is 100 percent attendance required?",
                ATTENDANCE,
                "75 percent",
            ),
        ):
            with self.subTest(question=question):
                response = ask(question, prose(1, passage))

                self.assertFalse(response.abstained)
                self.assertIn(expected, response.text)

    def test_an_amount_that_looks_like_a_year_does_not_make_a_rule_year_ful(self):
        """2000 is a year or an amount; beside "rupees" it is an amount, so the
        passage states no year and the rule is still year-less."""
        response = ask(
            "What is the library fine in 2025?",
            prose(1, "The library fine for a lost card is 2000 rupees."),
        )

        self.assertFalse(response.abstained)

    def test_a_year_the_passages_do_not_state_is_still_refused(self):
        bus = prose(
            1,
            "Campus bus services for the academic year 2024-25.\n"
            "A bus pass costs 4500 rupees per semester.",
        )

        for question in (
            "What does a bus pass cost for 2023-24?",
            "What does a bus pass cost in 2022?",
        ):
            with self.subTest(question=question):
                self.assertTrue(ask(question, bus).abstained)

        self.assertFalse(ask("What does a bus pass cost in 2025?", bus).abstained)
        self.assertFalse(ask("What does a bus pass cost for 2024-25?", bus).abstained)

    def test_a_range_inside_a_longer_stated_range_is_answered(self):
        response = ask(
            "What is the minimum attendance for 2023-24?",
            prose(
                1,
                "For the batches of 2019-2025 " + ATTENDANCE,
            ),
        )

        self.assertFalse(response.abstained)

    def test_a_date_in_another_year_than_the_one_the_passage_states_is_refused(self):
        response = ask(
            "Do I need to register before 15 August 2025?",
            prose(1, "A student must register before 15 August 2024."),
        )

        self.assertTrue(response.abstained)

    def test_year_facts_reads_years_ranges_and_dates(self):
        self.assertEqual(
            quoting.year_facts("the year 2024-25 and 15 August 2023"),
            ({"2024-2025"}, {"2024", "2025", "2023"}),
        )
        self.assertEqual(quoting.year_facts("Rs 2000 and 2000 rupees"), (set(), set()))
        self.assertEqual(quoting.year_facts("room 204, roll 12345"), (set(), set()))
        self.assertEqual(quoting.year_facts("15 August"), (set(), set()))


class CapitalisedWordTests(SimpleTestCase):
    """quote/F2: emphasis words and common acronyms are not codes."""

    FEE = (
        "Programme | Tuition | Exam Fee | Hostel\n"
        "B.Tech CSE | 85000 | 2500 | 60000\n"
        "M.Tech CSE | 90000 | 3000 | 60000"
    )
    WIFI = (
        "Each student account carries a monthly data allowance of 50 gigabytes. "
        "Beyond the allowance, access continues at a reduced speed."
    )
    PRINT = (
        "The printing allowance is 200 pages per semester. Additional pages are "
        "charged per page."
    )
    LEAVE = (
        "Earned leave accrues at 2.5 days per completed month of service. An "
        "application for earned leave must reach the head of department at least "
        "7 days in advance."
    )
    HOSTEL = (
        "The hostel curfew is 10 pm on weekdays and 11 pm on Saturdays. Residents "
        "must be inside by then."
    )
    LIB = "The library fine is 5 rupees per day per book for overdue books."

    def fee(self):
        return table(1, self.FEE)

    def test_the_reviewers_questions_are_answered(self):
        cases = (
            ("PLEASE tell me the tuition fee for B.Tech CSE", "fee", "85000"),
            ("Tell me the tuition fee for B.Tech CSE ASAP", "fee", "85000"),
            ("What is the tuition fee for B.Tech CSE? URGENT", "fee", "85000"),
            ("Is the WiFi quota 50 gigabytes a month?", "wifi", "50 gigabytes"),
            (
                "Can I print a PDF? What is the printing allowance?",
                "print",
                "200 pages",
            ),
            ("Who approves earned leave, the HOD?", "leave", "head of department"),
            ("What is the curfew on SUNDAYS in the hostel?", "host", "10 pm"),
            ("Is the library fine ONLY 5 rupees per day per book?", "lib", "5 rupees"),
            ("How much is the bus pass: INR 4,500 or less?", "bus", "4500 rupees"),
        )
        for question, key, expected in cases:
            with self.subTest(question=question):
                chunk = {
                    "fee": self.fee(),
                    "wifi": prose(2, self.WIFI),
                    "print": prose(3, self.PRINT),
                    "leave": prose(4, self.LEAVE),
                    "host": prose(5, self.HOSTEL),
                    "lib": prose(6, self.LIB),
                    "bus": prose(7, "A bus pass costs 4500 rupees per semester."),
                }[key]
                response = ask(question, chunk)

                self.assertFalse(response.abstained, response.metadata)
                self.assertIn(expected, response.text)

    def test_an_emphasis_word_is_not_a_code_even_beside_a_table_of_codes(self):
        """The fee table lists codes, so an acronym it lacks WOULD be refused;
        these words are ordinary English or abbreviations the documents spell
        out."""
        for word in ("PLEASE", "ASAP", "URGENT", "ONLY", "SUNDAYS", "PDF", "HOD"):
            with self.subTest(word=word):
                response = ask(
                    f"What is the tuition fee for B.Tech CSE? {word}", self.fee()
                )

                self.assertFalse(response.abstained)

    def test_a_word_the_passages_write_in_lower_case_is_an_ordinary_word(self):
        response = ask(
            "What is the SURCHARGE for B.Tech CSE?",
            prose(1, "A late payment surcharge of 500 rupees per week applies."),
            self.fee(),
        )

        self.assertFalse(response.abstained)

    def test_an_acronym_is_not_held_against_passages_with_no_table_of_codes(self):
        response = ask(
            "How many credits can I earn through NAAC approved courses?",
            prose(1, "A student may earn up to 12 credits through online courses."),
        )

        self.assertFalse(response.abstained)

    def test_a_code_the_fee_table_lacks_is_still_refused(self):
        for question in (
            "What is the tuition fee for the PhD programme?",
            "What is the tuition fee for Ph.D?",
            "What is the hostel fee for B.Arch students?",
            "What is the tuition fee for B.Tech EEE?",
            "What is the exam fee for MBA?",
        ):
            with self.subTest(question=question):
                response = ask(question, self.fee())

                self.assertTrue(response.abstained)
                self.assertEqual(response.text, ABSTENTION_TEXT)

    def test_a_code_in_the_table_is_still_answered(self):
        response = ask(
            "What is the tuition fee for MECH?",
            table(
                1,
                "Programme\tTuition\nB.Tech MECH\t78000",
                header=("Programme", "Tuition"),
            ),
        )

        self.assertFalse(response.abstained)
        self.assertIn("78000", response.text)

    def test_a_product_word_that_starts_in_lower_case_is_not_a_code(self):
        response = ask(
            "How many days may a digital eBook be borrowed?",
            prose(1, "A digital book may be borrowed for 14 days."),
        )

        self.assertFalse(response.abstained)

    def test_the_default_table_still_gives_the_base_cases(self):
        """The module-level fixtures are the project's own; this pins that the
        base file's table and header are what this module reuses."""
        self.assertIn("B.Tech CSE", FEE_TABLE)
        self.assertEqual(HEADER[0], "Programme")


class LabelAndValueLineTests(SimpleTestCase):
    """quote/F3: "Casual leave: 12 days" is a fact, not a heading."""

    LEAVE = (
        "Casual leave: 12 days per calendar year\n"
        "Earned leave: accrues 2.5 days per month\n"
        "Casual leave cannot be carried forward."
    )
    HOURS = (
        "Library Hours\n"
        "Monday to Saturday: 9 am to 8 pm\n"
        "Sunday: closed\n"
        "Hours are extended during examinations."
    )

    def test_a_label_and_value_line_is_quoted_over_a_weaker_sentence(self):
        response = ask(
            "How many days of casual leave per year?",
            prose(1, self.LEAVE, title="Leave Summary"),
        )

        self.assertIn("Casual leave: 12 days per calendar year", response.text)

    def test_the_hours_line_is_quoted_not_the_title_above_it(self):
        response = ask(
            "What are the library hours from Monday to Saturday?",
            prose(1, self.HOURS, title="Library Rules"),
        )

        self.assertIn("Monday to Saturday: 9 am to 8 pm", response.text)

    def test_which_lines_are_headings(self):
        def heading(text):
            return quoting.is_heading(text, is_row=False)

        for text in (
            "FEE STRUCTURE 2024-25",
            "Library Hours",
            "Clause 3.1 Visitors",
            "Clause 4.2: Attendance",
            "Subject: Campus bus services for the academic year 2024-25",
            "Institute of Technology - Office of the Bursar",
            "3. Library Rules",
            "4. ATTENDANCE",
            "Documents required for hostel admission",
        ):
            with self.subTest(text=text):
                self.assertTrue(heading(text))

        for text in (
            "Casual leave: 12 days per calendar year",
            "Monday to Saturday: 9 am to 8 pm",
            "Sunday: closed",
            "- Admission letter",
            "\N{BULLET} Fee receipt",
            "a) Admission letter",
            "(iv) two passport photographs",
            "2. Fee receipt",
            "3. Two passport photographs",
        ):
            with self.subTest(text=text):
                self.assertFalse(heading(text))

    def test_a_title_is_still_not_quoted_for_a_tuition_question(self):
        response = ask(
            "What is the tuition fee for B.Tech CSE?",
            prose(1, "FEE STRUCTURE 2024-25\nInstitute of Technology - Bursar"),
            table(2),
        )

        self.assertNotIn("FEE STRUCTURE", response.text)
        self.assertIn("85000", response.text)


class StrayTermTests(SimpleTestCase):
    """quote/F4: one shared word is not a second part of the answer."""

    def test_a_sentence_sharing_nothing_with_the_answer_is_not_appended(self):
        """It adds one term ("applied") and shares none with the answer. (One
        that also shares a word with the answer may be appended: that is the
        price of never dropping the sentence that holds the decisive term, which
        a 75-percent "already answered" rule did - see
        ASentenceThatHoldsTheAnswerIsNotDroppedTests.)"""
        response = ask(
            "How many days in advance must earned leave be applied?",
            staff(
                1,
                "Earned leave accrues at 2.5 days per completed month of service. "
                "An application for earned leave must reach the head of "
                "department at least 7 days in advance.",
            ),
            staff(
                2,
                "Deductions are shown separately on the monthly pay slip and are "
                "not refunded if a payment is later applied for.",
            ),
        )

        self.assertIn("7 days in advance", response.text)
        self.assertNotIn("Deductions", response.text)
        self.assertEqual(response.cited_markers, (1,))

    def test_a_term_every_sentence_of_the_document_shares_is_not_a_second_part(self):
        response = ask(
            "When is staff arrival considered late?",
            staff(1, "Arrival after 9.15 am is recorded as late attendance."),
            staff(
                2,
                "A staff member on probation must record a minimum of 90 percent "
                "attendance to be considered for confirmation.",
            ),
        )

        self.assertIn("9.15 am", response.text)
        self.assertNotIn("probation", response.text)

    def test_a_table_row_is_not_set_beside_a_prose_answer_on_one_word(self):
        response = ask(
            "What is the late payment surcharge for B.Tech?",
            prose(
                1,
                "A late payment surcharge of 500 rupees per week applies after "
                "that date.",
                title="Fee Structure 2024-25",
            ),
            table(2),
        )

        self.assertIn("surcharge of 500 rupees", response.text)
        self.assertNotIn("85000", response.text)

    def test_a_table_row_that_adds_two_terms_is_still_not_set_beside_prose(self):
        """B.Tech and CSE are two terms the surcharge sentence did not cover,
        and the row shares none with it: a tuition row is not part of the
        answer to the surcharge."""
        response = ask(
            "What is the late payment surcharge per week after that date for "
            "B.Tech CSE?",
            prose(
                1,
                "A late payment surcharge of 500 rupees per week applies after "
                "that date.",
                title="Fee Structure 2024-25",
            ),
            table(2),
        )

        self.assertIn("surcharge of 500 rupees", response.text)
        self.assertNotIn("85000", response.text)

    def test_a_question_about_two_rows_quotes_both_rows(self):
        response = ask("What is the fee for B.Tech CSE and B.Tech ECE?", table(1))

        self.assertIn("85000", response.text)
        self.assertIn("82000", response.text)

    def test_the_second_part_of_a_two_part_question_is_still_quoted(self):
        response = ask(
            "What is the minimum attendance for exams, and how many books can "
            "an undergraduate borrow?",
            prose(1, ATTENDANCE),
            prose(2, "An undergraduate student may borrow up to four books."),
        )

        self.assertIn("75 percent", response.text)
        self.assertIn("four books", response.text)

    def test_a_two_part_question_is_answered_in_a_document_about_its_topic(self):
        """ "scholarship" is in the document's title, and the second part still
        comes from the sentence about the merit scholarship."""
        response = ask(
            "What is the merit scholarship percentage and the attendance condition?",
            prose(
                1,
                "A scholarship holder must maintain a minimum of 85 percent "
                "attendance.",
                title="Scholarship and Financial Aid Rules",
            ),
            prose(
                2,
                "A student who secures an aggregate of 80 percent or above in the "
                "preceding academic year is eligible for the merit scholarship.",
                title="Scholarship and Financial Aid Rules",
            ),
        )

        self.assertIn("85 percent attendance", response.text)
        self.assertIn("eligible for the merit scholarship", response.text)

    def test_a_second_sentence_that_adds_one_term_and_shares_one_is_quoted(self):
        response = ask(
            "Does getting condonation for my attendance protect my scholarship?",
            prose(
                1,
                "A scholarship holder must maintain a minimum of 85 percent "
                "attendance.\n"
                "Condonation granted for examination purposes does not restore a "
                "scholarship.",
            ),
        )

        self.assertIn("does not restore a scholarship", response.text)


class CutStartTests(SimpleTestCase):
    """quote/F5: a lowercase brand word is not a word a boundary cut."""

    def test_a_real_word_or_label_is_never_dropped(self):
        for text in (
            "pH of the water sample is measured at 25 degrees.",
            "eBooks may be borrowed for 14 days.",
            "iPhone and Android devices must be registered.",
            "tV sets are not allowed in rooms.",
            "a) Admission letter",
            "i. the first condition",
            "e- mail is the official channel.",
        ):
            with self.subTest(text=text):
                self.assertEqual(quoting.trim_cut_start(text), (text, False))
                self.assertFalse(quoting.begins_mid_text(text))

    def test_the_end_of_a_cut_word_is_still_dropped_and_marked(self):
        self.assertEqual(
            quoting.trim_cut_start("f member is entitled to 12 days."),
            ("member is entitled to 12 days.", True),
        )
        self.assertTrue(quoting.begins_mid_text("f member is entitled to 12 days."))
        self.assertTrue(quoting.begins_mid_text("the next working day is used."))

    def test_a_sentence_that_starts_with_ph_is_quoted_whole_without_an_ellipsis(self):
        response = ask(
            "What is the pH measurement temperature?",
            prose(1, "pH of the water sample is measured at 25 degrees. Cold."),
        )

        self.assertEqual(
            response.text, "pH of the water sample is measured at 25 degrees. [1]"
        )

    def test_a_cut_start_is_still_marked_in_an_answer(self):
        response = ask(
            "How many days of casual leave do staff get?",
            prose(1, "f member is entitled to 12 days of casual leave in a year."),
        )

        self.assertTrue(response.text.startswith("\N{HORIZONTAL ELLIPSIS}member"))


class ExpletiveThenPronounTests(SimpleTestCase):
    """quote/F6: "Is it possible to extend it?" has a real second "it"."""

    def dependent(self, question):
        return followup.looks_dependent(question)[0]

    def test_a_real_pronoun_after_an_expletive_it_is_a_follow_up(self):
        for question in (
            "Is it possible to extend it?",
            "Is it required to pay it?",
            "Is it mandatory to pay it in full?",
            "Is it possible to renew it?",
            "Is it ok to pay it?",
        ):
            with self.subTest(question=question):
                self.assertTrue(self.dependent(question))

    def test_an_expletive_it_alone_is_still_independent(self):
        for question in (
            "Is it mandatory to register?",
            "Is it possible to defer?",
            "Is it mandatory to register with the placement cell?",
        ):
            with self.subTest(question=question):
                self.assertFalse(self.dependent(question))

    def test_it_with_a_subject_is_still_dependent_and_a_pronoun_with_a_noun_is_not(
        self,
    ):
        self.assertTrue(self.dependent("Is it allowed on weekends?"))
        self.assertTrue(self.dependent("Can I extend it?"))
        self.assertFalse(self.dependent("How do students renew their library cards?"))


class RefusalThatRepeatsTheQuestionTests(SimpleTestCase):
    """engine/F4: a refusal repeats the figure it was asked about."""

    QUESTION = "What is the tuition fee for 2023-24?"

    def test_a_refusal_that_restates_the_asked_year_is_a_refusal(self):
        for reply in (
            "I couldn't find the 2023-24 fee in these documents.",
            "The documents do not mention a fee for 2023-24.",
        ):
            with self.subTest(reply=reply):
                self.assertFalse(declines_to_answer(reply))
                self.assertTrue(declines_to_answer(reply, self.QUESTION))

    def test_a_figure_the_question_did_not_type_still_makes_it_an_answer(self):
        for reply in (
            "The documents do not mention 2023-24, but the 2024-25 fee is 85000.",
            "I couldn't find the 2023-24 fee. The fine is 50 rupees.",
            "I couldn't find the 2023-24 fee, but see [1].",
        ):
            with self.subTest(reply=reply):
                self.assertFalse(declines_to_answer(reply, self.QUESTION))

    def test_only_a_whole_run_of_digits_is_set_aside(self):
        """2024 is not 2023, and 202 is not in 2023 as a run."""
        self.assertFalse(
            declines_to_answer("I couldn't find the 2024 fee.", self.QUESTION)
        )
        self.assertFalse(
            declines_to_answer("I couldn't find a 202 fee.", self.QUESTION)
        )

    def test_without_a_question_nothing_changes(self):
        self.assertTrue(declines_to_answer("I couldn't find that in the documents."))
        self.assertFalse(declines_to_answer("The fine is 50 rupees, not found in [1]."))


BUS_CIRCULAR = (
    "Campus bus services for the academic year 2024-25.\n"
    "A bus pass costs 4500 rupees per semester.\n"
    "A lost pass is replaced on payment of 200 rupees."
)
LIBRARY_TEXT = "The late return fine is 5 rupees per day per book at the library."


class OwnSituationIsNotWhatIsAskedTests(SimpleTestCase):
    """Round two: a student who names their own programme or year while asking
    about a rule that applies to everyone was refused ('I am a B.Tech student,
    what is the library fine?' answered 5 of 32 against 26 before the rewrite).
    Who is asking is not what is asked."""

    def test_naming_ones_own_programme_does_not_refuse_a_general_rule(self):
        for question, passage, expected in (
            (
                "I am a B.Tech student. What is the library fine per day?",
                LIBRARY_TEXT,
                "5 rupees per day",
            ),
            (
                "As a PhD scholar, what is the minimum attendance?",
                ATTENDANCE,
                "75 percent",
            ),
            (
                "What is the minimum attendance for B.Tech students?",
                ATTENDANCE,
                "75 percent",
            ),
            (
                "Ph.D scholars: what is the minimum attendance?",
                ATTENDANCE,
                "75 percent",
            ),
        ):
            with self.subTest(question=question):
                response = ask(question, prose(1, passage))

                self.assertFalse(response.abstained, question)
                self.assertIn(expected, response.text)

    def test_a_fee_table_that_happens_to_be_retrieved_is_not_a_reason_to_refuse(self):
        """The table is keyed by programme, but nothing in the question asks for
        what its columns hold."""
        response = ask(
            "I am an EEE student. What is the library fine per day?",
            prose(1, LIBRARY_TEXT),
            table(2),
        )

        self.assertFalse(response.abstained)
        self.assertIn("5 rupees per day", response.text)

    def test_a_programme_asked_about_is_no_gap_for_a_table_not_asked_about(self):
        """The library fine is nobody's programme: the fee table beside it is keyed
        by programme but the question asks for nothing in its columns."""
        response = ask(
            "What is the library fine per day for B.Arch students?",
            prose(1, LIBRARY_TEXT),
            table(2),
        )

        self.assertFalse(response.abstained)
        self.assertIn("5 rupees per day", response.text)

    def test_as_a_programme_student_is_not_a_programme_asked_about(self):
        """Where the passages name programmes, one the question names is a gap -
        unless it is the asker's own ("As a B.Arch student, ...")."""
        passage = (
            "B.Tech tuition is 85000 per year and M.Tech tuition is 95000 per year.\n"
            "The hostel curfew is 10 pm on weekdays."
        )
        response = ask(
            "As a B.Arch student, what is the hostel curfew?", prose(1, passage)
        )

        self.assertFalse(response.abstained)
        self.assertIn("10 pm", response.text)

    def test_a_programme_asked_about_is_still_a_gap_where_programmes_are_listed(self):
        prose_fees = prose(
            3, "B.Tech tuition is 85000 per year and M.Tech tuition is 95000 per year."
        )
        for question, chunks in (
            ("What is the tuition fee for the PhD programme?", (table(1),)),
            ("What is the hostel fee for B.Arch students?", (table(1),)),
            ("What is the tuition fee for EEE?", (table(1),)),
            ("What is the tuition for B.Arch?", (prose_fees,)),
        ):
            with self.subTest(question=question):
                self.assertTrue(ask(question, *chunks).abstained, question)

    def test_emphasis_words_beside_a_code_table_are_not_codes(self):
        for question in (
            "CHECK what is the tuition fee for B.Tech CSE",
            "Is B.Tech CSE tuition 85000 per YEAR?",
            "PLEASE tell me the tuition fee for B.Tech CSE",
        ):
            with self.subTest(question=question):
                response = ask(question, table(1))

                self.assertFalse(response.abstained, question)
                self.assertIn("85000", response.text)

    def test_a_year_the_user_gives_about_themselves_is_not_a_year_asked_about(self):
        circular = prose(1, BUS_CIRCULAR)
        for question, expected in (
            (
                "I graduated in 2023. What is the lost bus pass replacement fee?",
                "200 rupees",
            ),
            ("I joined in 2022. How much does a bus pass cost?", "4500 rupees"),
            (
                "I paid 4500 for my bus pass in 2023, is that the right price?",
                "4500 rupees",
            ),
            ("I am in the 2021-25 batch. How much is the bus pass?", "4500 rupees"),
        ):
            with self.subTest(question=question):
                response = ask(question, circular)

                self.assertFalse(response.abstained, question)
                self.assertIn(expected, response.text)

    def test_a_programme_the_user_names_for_themselves_still_picks_its_row(self):
        """Only the terms of the own-situation clause that NO passage has are set
        aside ("am", "student", a batch): "ECE" is in the table, so it still
        selects the ECE row, and the other programmes' rows are not quoted."""
        for question in (
            "I am a B.Tech ECE student. What is my tuition fee?",
            "I am in the 2021-25 batch of B.Tech ECE. What is the tuition fee?",
        ):
            with self.subTest(question=question):
                response = ask(question, table(1))

                self.assertFalse(response.abstained, question)
                self.assertIn("82000", response.text)
                self.assertNotIn("85000", response.text)
                self.assertNotIn("95000", response.text)

    def test_the_asker_s_batch_does_not_sink_the_sentence_that_answers(self):
        """The words "am", "batch", "2021" and "25" are in no passage and in no
        question part: the sentence covering "bus pass" covers all that was asked."""
        for question in (
            "I am in the 2021-25 batch. How much is the bus pass?",
            "Hello, I am a 2021 batch student. How much is a bus pass?",
        ):
            with self.subTest(question=question):
                response = ask(question, prose(1, BUS_CIRCULAR))

                self.assertFalse(response.abstained, question)
                self.assertIn("4500 rupees per semester", response.text)

    def test_a_question_about_a_student_with_a_figure_is_all_question(self):
        """ "A student with 60 percent attendance" is own-situation text for the
        grounding rule, but a question made of it is not a statement the year in
        it could be set aside with."""
        circular = prose(1, BUS_CIRCULAR)
        for question in (
            "Can a student with 60 percent attendance get the 2023-24 bus pass?",
            "Is a student with 60 percent attendance charged for a 2022 bus pass?",
        ):
            with self.subTest(question=question):
                self.assertTrue(ask(question, circular).abstained, question)

    def test_a_year_asked_about_is_still_refused(self):
        circular = prose(1, BUS_CIRCULAR)
        for question in (
            "What does a bus pass cost for 2023-24?",
            "How much was the bus pass in 2022?",
        ):
            with self.subTest(question=question):
                self.assertTrue(ask(question, circular).abstained, question)


class TitleLinesAreNotFactsTests(SimpleTestCase):
    """Round two: 'Policy: Staff Leave Policy 2024-25' was classed as a fact
    ('label: value'), so the title was quoted and the sentence under it dropped."""

    POLICY = (
        "Policy: Staff Leave Policy 2024-25\n"
        "Staff may take 12 days of casual leave in a year."
    )

    def test_a_label_followed_by_a_title_is_a_heading(self):
        for line in (
            "Policy: Staff Leave Policy 2024-25",
            "Notice: Bus Pass Renewal for the Academic Year",
            "Circular: Revised Fee Structure 2024-25",
            "Guidelines: Use of the Computer Laboratory",
        ):
            with self.subTest(line=line):
                self.assertTrue(quoting.is_heading(line, is_row=False), line)

    def test_a_label_followed_by_a_value_is_a_fact(self):
        for line in (
            "Casual leave: 12 days per calendar year",
            "Monday to Saturday: 9 am to 8 pm",
            "Fee: Rs 500",
            "Warden: Dr Rao",
        ):
            with self.subTest(line=line):
                self.assertFalse(quoting.is_heading(line, is_row=False), line)

    def test_the_sentence_under_a_titled_line_is_quoted(self):
        response = ask("What is the staff leave policy?", prose(1, self.POLICY))

        self.assertFalse(response.abstained)
        self.assertIn("12 days of casual leave", response.text)


class ASentenceThatHoldsTheAnswerIsNotDroppedTests(SimpleTestCase):
    """Round two: a first sentence that shared most of the question's topic words
    made the second sentence, which adds the one decisive term, look like a
    leftover and dropped it, so the reader got an answer without the thing asked."""

    def test_the_sentence_with_the_decisive_term_is_added(self):
        response = ask(
            "What is the printing fee per page?",
            prose(
                1,
                "The printing allowance is 200 pages per semester.\n"
                "Additional pages cost a fee of 2 rupees per page.",
            ),
        )

        self.assertIn("200 pages per semester", response.text)
        self.assertIn("fee of 2 rupees per page", response.text)


class WhatTheAnswerAlreadyHasIsNotAddedAgainTests(SimpleTestCase):
    """The offline v2 gate caught the cost of the rule above: a sentence that adds
    ONE term and shares another with the first was appended even when the term was
    the asker's own figure or the same word in another form."""

    CONDONATION = (
        "A student with attendance between 65 and 75 percent may apply for "
        "condonation on medical grounds, supported by a certificate.\n"
        "A student with an aggregate below 60 percent, or with a pending backlog, "
        "is not eligible for a scholarship."
    )
    AID = (
        "Families whose annual income is below 300000 rupees may apply for aid, "
        "which covers the examination fee in full."
    )

    def test_a_number_the_asker_gave_about_themselves_is_not_a_second_part(self):
        response = ask(
            "Can a student with 60 percent attendance apply for condonation?",
            prose(1, self.CONDONATION),
        )

        self.assertFalse(response.abstained)
        self.assertIn("apply for condonation", response.text)
        self.assertNotIn("aggregate below 60 percent", response.text)

    def test_a_number_that_is_asked_about_still_picks_its_sentence(self):
        """Not the asker's own: nothing says "I" or "student with"."""
        response = ask(
            "What happens to attendance of 60 percent and condonation?",
            prose(1, self.CONDONATION),
        )

        self.assertIn("apply for condonation", response.text)
        self.assertIn("below 60 percent", response.text)

    def test_the_same_word_in_another_form_is_not_a_part_still_missing(self):
        """ "examination" is not "exam": the stemmer does not join them, and the
        row under "Exam Fee" was set beside an aid sentence for the other form."""
        response = ask(
            "What is the examination fee for M.Tech CSE?",
            table(1),
            prose(2, self.AID),
        )

        self.assertIn("95000", response.text)
        self.assertNotIn("annual income", response.text)

    def test_variants_the_stemmer_does_not_join(self):
        for term, others in (
            ("examination", {"exam", "fee"}),
            ("exam", {"examination"}),
            ("charge", {"charg"}),
            ("booking", {"book"}),
        ):
            with self.subTest(term=term):
                self.assertTrue(quoting.is_variant_of(term, others))

    def test_things_that_are_not_variants(self):
        for term, others in (
            ("fee", {"feed"}),  # under four letters
            ("hostel", {"hostelsandmoreinfo"}),  # more than seven letters longer
            ("2021", {"20215"}),  # numbers are never variants
            ("fee", {"fee"}),  # the same word is covered, not a variant
            ("tuition", {"hostel"}),
            ("exam", set()),
        ):
            with self.subTest(term=term, others=others):
                self.assertFalse(quoting.is_variant_of(term, others))

    def test_the_figures_a_user_gives_about_themselves(self):
        self.assertEqual(
            quoting.own_figure_terms(
                "Can a student with 60 percent attendance apply for condonation?"
            ),
            frozenset({"60"}),
        )
        self.assertEqual(
            quoting.own_figure_terms("I have 70 percent. What is the minimum?"),
            frozenset({"70"}),
        )
        self.assertEqual(
            quoting.own_figure_terms("Is the minimum 70 percent?"), frozenset()
        )
