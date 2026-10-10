"""Phase 12: query planning, and the measurement that justified it.

The planner tests are pure - planning is a function of the question text.
The runner tests go through the real engine over a real corpus, because the
only claim worth making about planning is a measured one.
"""

import re
from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from documents import agents
from documents.agents import planner
from documents.models import Document, ProcessingJob
from documents.processing.pipeline import run_pipeline
from documents.rag.engine import RagAnswer
from documents.tests import MEDIA_ROOT, make_user

CORPUS = {
    "Regulations": "ACADEMIC REGULATIONS R20\n"
    "Clause 4.2 Attendance minimum is 75 percent for examination eligibility.\n",
    "Library": "LIBRARY RULES\n"
    "Clause 9.1 A student may borrow up to four books at one time.\n"
    "Clause 9.4 The late return fine is 5 rupees per day.\n",
    "Hostel": "HOSTEL REGULATIONS\nClause 3.1 The hostel curfew is 10 pm on weekdays.\n",
    "Transport": "TRANSPORT POLICY\n"
    "Clause 7.2 The bus pass costs 12000 rupees per year.\n",
}

THREE_PART = (
    "What is the attendance minimum, how many books can a student borrow, "
    "and what is the hostel curfew?"
)
FOUR_PART = (
    "What is the attendance minimum, what is the late return fine, "
    "what is the hostel curfew, and what does the bus pass cost?"
)
TWO_PART = "What is the hostel curfew and what is the late return fine?"


class PlannerTests(TestCase):
    def test_a_single_question_is_one_step(self):
        self.assertEqual(
            planner.plan("What is the attendance minimum?"),
            ["What is the attendance minimum?"],
        )

    def test_a_two_part_question_is_deliberately_not_split(self):
        """Measured: two-part questions already answer both parts. Splitting
        them would spend a second retrieval to reach the same answer."""
        self.assertEqual(planner.plan(TWO_PART), [TWO_PART])

    def test_a_three_part_question_splits_into_three(self):
        self.assertEqual(
            planner.plan(THREE_PART),
            [
                "What is the attendance minimum?",
                "how many books can a student borrow?",
                "what is the hostel curfew?",
            ],
        )

    def test_a_four_part_question_splits_into_four(self):
        self.assertEqual(len(planner.plan(FOUR_PART)), 4)

    def test_the_split_uses_every_boundary_not_just_the_first_kind(self):
        """The bug this catches: ", and " matched first, produced two
        parts, and the plain comma earlier in the sentence was never
        considered - so a three-part question planned as two, was then
        left unplanned for being two, and the failure went unfixed while
        every test passed."""
        parts = planner.plan(THREE_PART)

        self.assertEqual(len(parts), 3)
        self.assertTrue(all("," not in part for part in parts))

    def test_a_later_part_inherits_the_opening_interrogative(self):
        """ "the tuition" retrieves far worse than "What is the tuition"."""
        parts = planner.plan(
            "What is the attendance minimum, the hostel curfew, and the bus fare?"
        )

        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertTrue(part.lower().startswith("what is"), part)

    def test_a_conjunction_inside_one_question_is_not_a_split(self):
        """ "late return and damage" is one question, not two."""
        question = "What is the fine for late return and damage?"

        self.assertEqual(planner.plan(question), [question])

    def test_planning_can_be_switched_off(self):
        self.assertEqual(planner.plan(THREE_PART, max_steps=1), [THREE_PART])

    def test_more_parts_than_the_bound_are_not_planned(self):
        """A bound, not a budget: each step is a full retrieval."""
        many = (
            "What is the attendance minimum, what is the late fine, "
            "what is the curfew, what is the bus fare, and what is the tuition?"
        )

        self.assertEqual(planner.plan(many, max_steps=4), [many])

    def test_an_empty_question_plans_nothing(self):
        self.assertEqual(planner.plan(""), [])
        self.assertEqual(planner.plan("   "), [])


class MarkerOffsetTests(SimpleTestCase):
    """Renumbering when a step does not cite its passages from [1] up."""

    @staticmethod
    def step(text, cited):
        """A grounded step's answer, citing {marker: chunk_id}."""
        return RagAnswer(
            question="q",
            answer=text,
            abstained=False,
            citations=[
                {"marker": marker, "chunk_id": chunk_id}
                for marker, chunk_id in cited.items()
            ],
            support_ratio=1.0,
            model_name="extractive-answerer",
        )

    def test_a_step_citing_a_later_passage_does_not_share_a_marker(self):
        """Offsetting by the number of citations so far: a first step that
        cited only its [2] moved the second step's [1] to [2] as well, so
        one marker named two passages and the reader was sent to one of
        them for both claims."""
        steps = [
            self.step("Attendance minimum is 75 percent. [2]", {2: 12}),
            self.step("A student may borrow up to four books. [1]", {1: 21}),
            self.step("The curfew is 10 pm [1] on weekdays [3].", {1: 31, 3: 33}),
        ]

        with mock.patch("documents.agents.runner.ask", side_effect=steps):
            planned = agents.answer(None, THREE_PART)

        resolves_to = {c["marker"]: c["chunk_id"] for c in planned.citations}
        self.assertEqual(len(resolves_to), len(planned.citations), planned.citations)

        # Each part's markers resolve to the chunks that part's own step cited.
        for paragraph, expected in zip(
            planned.answer.split("\n\n"), ({12}, {21}, {31, 33}), strict=True
        ):
            markers = {int(value) for value in re.findall(r"\[(\d+)\]", paragraph)}
            self.assertEqual({resolves_to[m] for m in markers}, expected, paragraph)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PlanRunnerTests(TestCase):
    """The measured claim: 3 parts went from 1 of 3 to 3 of 3, and 4 parts
    from a flat abstention to 4 of 4."""

    def setUp(self):
        self.user = make_user("plan_user", department="Registry")

        for title, body in CORPUS.items():
            document = Document.objects.create(
                title=title,
                document_type="OTHER",
                owner=self.user,
                department="Registry",
            )
            version = document.versions.create(
                version_number=1,
                file=ContentFile(body.encode(), name="d.txt"),
                file_name="d.txt",
                file_size=len(body),
                uploaded_by=self.user,
            )
            document.current_version = version
            document.save(update_fields=["current_version"])
            run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def test_a_three_part_question_is_answered_in_three_parts(self):
        from documents import rag

        single = rag.ask(self.user, THREE_PART)
        planned = agents.answer(self.user, THREE_PART)

        expected = ["75 percent", "four books", "10 pm"]
        single_found = [item for item in expected if item in single.answer]
        planned_found = [item for item in expected if item in planned.answer]

        self.assertLess(
            len(single_found),
            len(expected),
            "the fixture no longer exercises the single-shot failure",
        )
        self.assertEqual(len(planned_found), len(expected), planned.answer)

    def test_a_four_part_question_no_longer_abstains_outright(self):
        from documents import rag

        single = rag.ask(self.user, FOUR_PART)
        planned = agents.answer(self.user, FOUR_PART)

        self.assertTrue(
            single.abstained,
            "the fixture no longer exercises the four-part abstention",
        )
        self.assertFalse(planned.abstained)
        for expected in ("75 percent", "5 rupees", "10 pm", "12000"):
            self.assertIn(expected, planned.answer)

    def test_markers_are_renumbered_into_one_sequence(self):
        """Step two's [1] and step one's [1] are different passages. A
        marker that resolves to the wrong passage looks checkable and is
        not."""
        planned = agents.answer(self.user, FOUR_PART)
        markers = [citation["marker"] for citation in planned.citations]

        self.assertEqual(markers, sorted(markers))
        self.assertEqual(len(markers), len(set(markers)))

    def test_every_marker_in_the_text_resolves_to_a_citation(self):
        import re

        planned = agents.answer(self.user, FOUR_PART)
        cited = {citation["marker"] for citation in planned.citations}
        used = {int(value) for value in re.findall(r"\[(\d+)\]", planned.answer)}

        self.assertTrue(used)
        self.assertTrue(
            used <= cited, f"markers {sorted(used - cited)} resolve to nothing"
        )

    def test_an_unanswered_part_is_reported_rather_than_dropped(self):
        question = (
            "What is the attendance minimum, what is the hostel curfew, "
            "and what is the parking policy for visiting lecturers?"
        )

        planned = agents.answer(self.user, question)

        self.assertTrue(planned.unanswered)
        self.assertIn("parking", planned.unanswered[0].lower())
        self.assertTrue(any("not answered" in note for note in planned.notes))

    def test_a_question_nobody_can_answer_abstains(self):
        question = (
            "What is the parking policy, what is the catering contract, "
            "and what is the lift maintenance schedule?"
        )

        planned = agents.answer(self.user, question)

        self.assertTrue(planned.abstained)
        self.assertEqual(len(planned.unanswered), 3)

    def test_a_single_question_takes_the_unplanned_path(self):
        planned = agents.answer(self.user, "What is the attendance minimum?")

        self.assertFalse(planned.planned)
        self.assertEqual(len(planned.steps), 1)
        self.assertIn("75 percent", planned.answer)

    def test_planning_never_widens_what_a_caller_can_see(self):
        """Each step is an ordinary rag.ask, so the permission filter runs
        inside every one of them. A planned question must not reach a
        document a single one could not."""
        outsider = make_user("plan_outsider", department="Physics")

        planned = agents.answer(outsider, FOUR_PART)

        self.assertTrue(planned.abstained)
        self.assertEqual(planned.citations, [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ChatPlanningTests(TestCase):
    def setUp(self):
        self.user = make_user("plan_api_user", department="Registry")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        for title, body in CORPUS.items():
            document = Document.objects.create(
                title=title,
                document_type="OTHER",
                owner=self.user,
                department="Registry",
            )
            version = document.versions.create(
                version_number=1,
                file=ContentFile(body.encode(), name="d.txt"),
                file_name="d.txt",
                file_size=len(body),
                uploaded_by=self.user,
            )
            document.current_version = version
            document.save(update_fields=["current_version"])
            run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def ask(self, question):
        return self.client.post(reverse("chat"), {"question": question}, format="json")

    def test_the_endpoint_reports_whether_it_planned(self):
        response = self.ask(FOUR_PART)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["plan"]["planned"])
        self.assertEqual(len(response.data["plan"]["steps"]), 4)

    def test_a_single_question_reports_that_it_was_not_planned(self):
        response = self.ask("What is the attendance minimum?")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["plan"]["planned"])
        self.assertIn("75 percent", response.data["answer"])

    def test_the_stored_message_keeps_the_plan(self):
        from documents.models import Message

        self.ask(FOUR_PART)

        message = Message.objects.filter(role=Message.Role.ASSISTANT).latest("id")
        self.assertTrue(message.metadata["planned"])
        self.assertEqual(len(message.metadata["plan"]), 4)

    def follow_up(self, first, question):
        opening = self.ask(first)
        return self.client.post(
            reverse("chat"),
            {"question": question, "conversation_id": opening.data["conversation_id"]},
            format="json",
        )

    def test_a_multi_part_follow_up_is_planned_on_the_users_own_words(self):
        """A widened follow-up is "<words> <borrowed terms>". Planned on that
        string, the borrowed terms split off at the "? " into a part that
        re-asked the previous turn; and with the whole question as every
        step's focus, each step's correct sentence was refused."""
        response = self.follow_up(
            "What is the attendance minimum?",
            "What about the hostel curfew, the late return fine and the bus pass cost?",
        )

        self.assertEqual(response.status_code, 200)
        steps = response.data["plan"]["steps"]
        self.assertEqual(len(steps), 3, [step["question"] for step in steps])
        self.assertNotIn("attendance", " ".join(step["question"] for step in steps))
        for expected in ("10 pm", "5 rupees", "12000"):
            self.assertIn(expected, response.data["answer"])
        self.assertEqual(response.data["plan"]["unanswered"], [])

    def test_a_two_part_follow_up_stays_one_question(self):
        """Two parts are not planned - measured to work as one retrieval. The
        borrowed terms used to turn them into three."""
        response = self.follow_up(
            "What is the attendance minimum?",
            "What about the hostel curfew and the late return fine?",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["plan"]["planned"])
