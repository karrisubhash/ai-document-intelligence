"""What the quoting answerer quotes when no model can write the answer.

This is the answer a reader gets whenever the AI is at capacity or down, so on
a free tier it is shown every day. These pin the mistakes it was seen making
(measured on the v3 benchmark with the live site's real embeddings and
reranker, every model call failing):

- a document TITLE quoted as the answer ("FEE STRUCTURE 2024-25 [1]" for a
  tuition question, "Institute of Technology - Training and Placement Cell"
  for a deadline);
- one half of a two-part question answered and the other dropped, because the
  coverage floor was measured over BOTH parts' terms;
- a table row skipped because its question named the column ("tuition") that
  only the header row spells;
- a sentence cut mid-word at a chunk boundary ("f member is entitled...");
- a whole fee table dumped for a programme (PhD, B.Arch) that is not in it,
  and a bus pass price for 2023-24 given from the 2024-25 circular.
"""

from django.test import SimpleTestCase

from documents.rag.llm import ABSTENTION_TEXT, ExtractiveAnswerer
from documents.tests_rag_tables import context_of, make_chunk

HEADER = ("Programme", "Tuition", "Exam Fee", "Hostel")
FEE_TABLE = (
    "Programme\tTuition\tExam Fee\tHostel\n"
    "B.Tech CSE\t85000\t2500\t60000\n"
    "B.Tech ECE\t82000\t2500\t60000\n"
    "M.Tech CSE\t95000\t3000\t60000"
)


def ask(question, *chunks, focus=""):
    built = context_of(*chunks)
    built.focus = focus
    return ExtractiveAnswerer().answer(question=question, context=built, prompt="")


def prose(identifier, text, title="Doc"):
    return make_chunk(identifier, text, title=title, table=False)


def table(identifier, text=FEE_TABLE, title="Fee Structure 2024-25", header=HEADER):
    return make_chunk(identifier, text, title=title, table=True, header=header)


class HeadingsAreNotAnswersTests(SimpleTestCase):
    def test_a_title_line_is_not_quoted_when_a_clause_answers(self):
        response = ask(
            "By which date must a student register with the placement cell?",
            prose(
                1,
                "PLACEMENT AND INTERNSHIP POLICY\n"
                "Institute of Technology - Training and Placement Cell\n"
                "Clause 1.3 Registration\n"
                "A student must register with the cell before 15 August of the "
                "final year.\n"
                "Registration is not automatic.",
            ),
        )

        self.assertFalse(response.abstained)
        self.assertIn("15 August", response.text)
        self.assertNotIn("PLACEMENT AND INTERNSHIP POLICY", response.text)
        self.assertNotIn("Institute of Technology", response.text)

    def test_a_clause_heading_is_not_quoted_beside_its_clause(self):
        """The heading matched all three terms and the clause two, so the
        heading - a shorter line - came first and the figure never did."""
        response = ask(
            "What is the late return fine?",
            prose(
                1,
                "Clause 9.4 Late return fine\n"
                "A charge of 5 rupees per day per book applies to every late "
                "return.",
            ),
        )

        self.assertIn("5 rupees", response.text)
        self.assertNotIn("Clause 9.4", response.text)

    def test_a_heading_cannot_answer_a_question_for_a_date(self):
        """The paraphrase ("sign up" for "register") shares no word with the
        clause, so only the office's name matched - and it was quoted as the
        answer to "by which date"."""
        response = ask(
            "By which date must I sign up with the placement cell?",
            prose(
                1,
                "Institute of Technology - Training and Placement Cell\n"
                "A student must register with the cell before 15 August of the "
                "final year.",
            ),
        )

        self.assertTrue(response.abstained)
        self.assertNotIn("Institute of Technology", response.text)

    def test_a_heading_is_still_quoted_when_it_is_all_that_answers(self):
        """The circular's subject line IS the answer to "which year"."""
        response = ask(
            "Which academic year does the bus circular cover?",
            prose(
                1,
                "Subject: Campus bus services for the academic year 2024-25\n"
                "1. Routes and timings\n"
                "Buses run on six city routes.",
            ),
        )

        self.assertFalse(response.abstained)
        self.assertIn("2024-25", response.text)


class HeadingsThatStateTheAnswerTests(SimpleTestCase):
    def test_a_heading_that_states_the_year_answers_a_year_question(self):
        """Demoting every heading cost this answer: the subject line states
        the year, and a two-word coincidence about "following year" beat it."""
        response = ask(
            "Which academic year does the bus circular cover?",
            prose(
                1,
                "Subject: Campus bus services for the academic year 2024-25\n"
                "1. Routes and timings",
            ),
            prose(
                2,
                "The award covers 50 percent of tuition for the following year.",
            ),
        )

        # First, not alone: "covers" and "cover" share a stem, and a sentence
        # that matches a term the heading did not is quoted after it.
        self.assertTrue(response.text.startswith("Subject: Campus bus services"))
        self.assertIn("2024-25", response.text)

    def test_a_clause_number_is_not_a_figure_a_heading_states(self):
        response = ask(
            "When is the supplementary examination held?",
            prose(
                1,
                "Clause 12.5 Supplementary examination\n"
                "A supplementary examination is conducted once per academic "
                "year, in July.",
            ),
        )

        self.assertTrue(response.text.startswith("A supplementary examination"))
        self.assertNotIn("Clause 12.5", response.text)


class AnswerTypeTests(SimpleTestCase):
    def test_a_how_much_question_prefers_the_sentence_with_a_figure(self):
        response = ask(
            "How much is the late fine?",
            prose(
                1,
                "The late fine policy is explained in the library rules.\n"
                "The late return fine is 5 rupees per day per book.",
            ),
        )

        self.assertTrue(response.text.startswith("The late return fine is 5 rupees"))

    def test_a_when_question_prefers_the_sentence_with_a_date(self):
        response = ask(
            "When must a student register with the cell?",
            prose(
                1,
                "Every student should register with the cell using the online "
                "form.\n"
                "A student must register with the cell before 15 August.",
            ),
        )

        self.assertTrue(response.text.startswith("A student must register"))

    def test_a_question_that_wants_no_figure_keeps_the_shorter_sentence(self):
        """The bias is for questions that ask for a figure or a date; for any
        other the tie-break is what it always was."""
        response = ask(
            "What is the library policy?",
            prose(
                1,
                "The library policy covers borrowing, returns and fines for 2024.\n"
                "The library policy is published.",
            ),
        )

        self.assertTrue(response.text.startswith("The library policy is published"))


class TableRowTests(SimpleTestCase):
    def test_a_row_is_matched_with_the_columns_it_sits_under(self):
        """ "tuition" is spelled only by the header row, so the ECE row matched
        two of the question's terms and was never quoted."""
        response = ask("What is the B.Tech ECE tuition fee?", table(1))

        self.assertFalse(response.abstained)
        self.assertIn("B.Tech ECE\t82000", response.text)
        self.assertIn(
            "(table columns: Programme | Tuition | Exam Fee | Hostel)", response.text
        )

    def test_a_title_does_not_outrank_the_row_that_answers(self):
        response = ask(
            "What is the B.Tech ECE tuition fee for 2024-25?",
            table(
                1,
                "FEE STRUCTURE 2024-25\n" + FEE_TABLE,
            ),
        )

        self.assertIn("B.Tech ECE\t82000", response.text)
        self.assertNotIn("FEE STRUCTURE", response.text)

    def test_only_the_rows_that_name_something_asked_are_quoted(self):
        response = ask("What is the B.Tech ECE tuition fee?", table(1))

        self.assertNotIn("B.Tech CSE", response.text)
        self.assertNotIn("M.Tech", response.text)

    def test_a_question_that_names_only_a_column_still_quotes_the_whole_table(self):
        """Nothing in a row was named, so no row is the answer; the table is."""
        response = ask("What is the exam fee?", table(1))

        self.assertFalse(response.abstained)
        self.assertIn("Programme", response.text)
        self.assertIn("B.Tech CSE", response.text)
        self.assertIn("M.Tech CSE", response.text)

    def test_a_row_is_found_by_its_label_and_a_column_alone(self):
        response = ask("What is the fee for ECE?", table(1))

        self.assertFalse(response.abstained)
        self.assertIn("B.Tech ECE", response.text)


class CompoundQuestionTests(SimpleTestCase):
    ATTENDANCE = prose(
        1,
        "A student must maintain a minimum of 75 percent attendance in each "
        "course to be eligible for the end-semester examination.",
    )
    LIBRARY = prose(
        2, "An undergraduate student may borrow up to four books at one time."
    )

    def test_both_parts_of_a_two_part_question_are_answered(self):
        """The attendance sentence matched two of the question's six terms,
        under the coverage floor measured over BOTH parts."""
        response = ask(
            "What is the minimum attendance for exams, and how many books can "
            "an undergraduate borrow?",
            self.ATTENDANCE,
            self.LIBRARY,
        )

        self.assertIn("75 percent", response.text)
        self.assertIn("four books", response.text)
        self.assertEqual(response.cited_markers, (1, 2))

    def test_a_second_good_sentence_that_adds_a_term_is_quoted(self):
        """It matched one term the first did not, and was quoted before the
        selection began to ask for two."""
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

        self.assertIn("85 percent attendance", response.text)
        self.assertIn("does not restore a scholarship", response.text)

    def test_a_single_topic_question_is_not_padded_with_the_same_thing(self):
        response = ask(
            "What is the attendance minimum for examination?",
            prose(
                1,
                "Attendance minimum is 75 percent for examination.\n"
                "Attendance below 75 percent bars a student from examination.",
            ),
        )

        self.assertIn("Attendance minimum is 75 percent", response.text)
        self.assertNotIn("bars a student", response.text)

    def test_a_sentence_sharing_only_the_words_last_day_is_not_a_second_part(self):
        """ "All fees are payable before the last working day of July" shares
        "last" and "day" with "the last day to submit applications for aid",
        and was quoted beside the real answer as if it were another part."""
        response = ask(
            "What is the last day to submit applications for financial aid?",
            prose(1, "Applications for all financial aid close on 30 September."),
            prose(2, "All fees are payable before the last working day of July."),
        )

        self.assertIn("close on 30 September", response.text)
        self.assertNotIn("working day of July", response.text)
        self.assertEqual(response.cited_markers, (1,))

    def test_the_same_words_still_count_when_the_question_is_not_for_a_date(self):
        """Only a question for a date treats "day" as the shape of the answer."""
        response = ask(
            "What is the library fine for a late book, and the charge per day?",
            prose(1, "The library fine for a late book is 5 rupees."),
            prose(2, "Each day a book is late attracts the fine under the rules."),
        )

        self.assertIn("5 rupees", response.text)
        self.assertIn("Each day a book is late", response.text)

    def test_a_second_sentence_must_add_two_terms_the_first_did_not_cover(self):
        response = ask(
            "What is the minimum attendance for exams, and how many books can "
            "an undergraduate borrow?",
            self.LIBRARY,
            prose(3, "The cafeteria opens early on weekdays for attendance."),
        )

        self.assertIn("four books", response.text)
        self.assertNotIn("cafeteria", response.text)


class CutSentenceTests(SimpleTestCase):
    def test_a_sentence_cut_mid_word_at_a_chunk_boundary_is_marked_and_trimmed(self):
        response = ask(
            "How many days of casual leave do staff get?",
            prose(
                1,
                "f member is entitled to 12 days of casual leave in a calendar "
                "year. Casual leave cannot be carried forward.",
            ),
        )

        self.assertIn("12 days of casual leave", response.text)
        self.assertTrue(
            response.text.startswith("\N{HORIZONTAL ELLIPSIS}member is entitled")
        )
        self.assertNotIn("f member", response.text)

    def test_a_sentence_that_starts_cleanly_is_not_marked(self):
        response = ask(
            "How many days of casual leave do staff get?",
            prose(1, "Each staff member is entitled to 12 days of casual leave."),
        )

        self.assertFalse(response.text.startswith("\N{HORIZONTAL ELLIPSIS}"))


class SentenceEndTests(SimpleTestCase):
    def test_a_sentence_closed_by_a_quote_is_not_marked_as_cut_off(self):
        response = ask(
            "What is the late fine per book?",
            prose(
                1,
                'The rules state that "the late return fine is 5 rupees per day '
                'per book for each overdue item borrowed from the central library."',
            ),
        )

        self.assertIn("5 rupees", response.text)
        self.assertNotIn("…", response.text)

    def test_a_long_sentence_that_just_stops_is_marked_as_cut_off(self):
        response = ask(
            "How much does a bus pass cost?",
            prose(
                1,
                "A bus pass costs 4500 rupees per semester and is issued on "
                "payment at the Transport Section during the first week of the",
            ),
        )

        self.assertIn("4500 rupees", response.text)
        self.assertIn("the… [1]", response.text)


class WrongEntityTests(SimpleTestCase):
    def test_a_programme_that_is_not_in_the_table_is_not_answered_with_the_table(self):
        for question in (
            "What is the tuition fee for the PhD programme?",
            "What is the hostel fee for B.Arch students?",
        ):
            with self.subTest(question=question):
                response = ask(question, table(1))

                self.assertTrue(response.abstained)
                self.assertEqual(response.text, ABSTENTION_TEXT)

    def test_a_programme_that_is_in_the_table_is_answered(self):
        response = ask(
            "What is the tuition fee for the B.Tech CSE programme?", table(1)
        )

        self.assertFalse(response.abstained)
        self.assertIn("85000", response.text)

    def test_an_identifier_in_a_title_counts_as_present(self):
        response = ask(
            "What is the exam fee for the MECH programme?",
            table(
                1,
                "Programme\tExam Fee\nB.Tech\t2500",
                title="Fee Structure MECH 2024-25",
                header=("Programme", "Exam Fee"),
            ),
        )

        self.assertFalse(response.abstained)

    def test_a_year_the_documents_do_not_mention_is_not_answered_from_another(self):
        bus = prose(
            1,
            "Campus bus services for the academic year 2024-25.\n"
            "A bus pass costs 4500 rupees per semester.",
        )

        refused = ask("What does a bus pass cost for the 2023-24 year?", bus)
        answered = ask("What does a bus pass cost for 2024-25?", bus)

        self.assertTrue(refused.abstained)
        self.assertFalse(answered.abstained)
        self.assertIn("4500 rupees", answered.text)

    def test_a_date_written_another_way_is_still_the_same_date(self):
        response = ask(
            "What happens on 31 March 2025?",
            prose(1, "All fees must be paid on 31.03.2025 without exception."),
        )

        self.assertFalse(response.abstained)

    def test_ordinary_capitalised_words_are_not_identifiers(self):
        """Only a dotted code, a mixed-case code or an acronym of three
        letters or more is - not a word that starts a sentence or names a day."""
        response = ask(
            "What is the Hostel curfew on Saturdays?",
            prose(1, "The curfew is 11 pm on Saturdays for hostel residents."),
        )

        self.assertFalse(response.abstained)
        self.assertIn("11 pm", response.text)

    def test_a_question_typed_in_capitals_names_nothing(self):
        """Every word of it would be an acronym."""
        response = ask("WHAT IS THE TUITION FEE FOR B.TECH CSE?", table(1))

        self.assertFalse(response.abstained)
        self.assertIn("85000", response.text)

    def test_a_short_acronym_is_not_held_against_a_paraphrase(self):
        """ "IT lab" for "computer lab": two letters is too short to be sure."""
        response = ask(
            "Is the IT lab open on Saturdays?",
            prose(1, "The computer lab is open on Saturdays until noon."),
        )

        self.assertFalse(response.abstained)

    def test_a_follow_up_is_judged_on_the_users_own_words(self):
        """The borrowed terms came from an earlier turn; an identifier among
        them is not something THIS question names."""
        response = ask(
            "What about condonation? attendance PhD examination",
            prose(
                1,
                "A student with attendance between 65 and 75 percent may apply "
                "for condonation on medical grounds.",
            ),
            focus="What about condonation?",
        )

        self.assertFalse(response.abstained)


class UnchangedTests(SimpleTestCase):
    def test_a_plain_answer_is_quoted_verbatim_with_its_marker(self):
        response = ask(
            "What is the minimum attendance?",
            prose(1, "Clause 4.2 Attendance minimum is 75 percent for eligibility."),
        )

        self.assertFalse(response.abstained)
        self.assertEqual(
            response.text,
            "Clause 4.2 Attendance minimum is 75 percent for eligibility. [1]",
        )

    def test_nothing_matching_is_still_a_refusal(self):
        response = ask(
            "What is the policy on cryptocurrency mining?",
            prose(1, "Borrowing limit is six books per student for fourteen days."),
        )

        self.assertTrue(response.abstained)
