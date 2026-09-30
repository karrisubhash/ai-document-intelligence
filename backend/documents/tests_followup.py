"""Resolving a follow-up against the conversation it belongs to.

The behaviour these pin is narrow and the failure mode on either side is
expensive, which is why both sides are tested:

  * A follow-up that is NOT resolved retrieves on two or three words and
    abstains on a question the corpus answers.
  * An independent question that IS resolved retrieves on the previous
    turn's vocabulary as well as its own, and quietly answers from the
    wrong document - which is worse, because it does not look like a
    failure.
"""

from dataclasses import dataclass

from django.test import SimpleTestCase, override_settings

from documents.rag import followup


@dataclass
class Turn:
    """Shaped like a Message, without needing a database for it."""

    role: str
    content: str


def history(*pairs):
    """Turns oldest first, as the conversation would store them."""
    turns = []
    for question, answer in pairs:
        turns.append(Turn(role="USER", content=question))
        if answer is not None:
            turns.append(Turn(role="ASSISTANT", content=answer))
    return turns


class DependenceDetectionTests(SimpleTestCase):
    def test_elliptical_follow_ups_are_dependent(self):
        for question in (
            "What about MECH?",
            "And for ECE?",
            "How about part-time students?",
            "Is that per semester?",
            "What if I miss it?",
        ):
            with self.subTest(question=question):
                dependent, reason = followup.looks_dependent(question)
                self.assertTrue(dependent, f"{question!r} should be dependent")
                self.assertTrue(reason)

    def test_a_long_question_referring_backwards_is_dependent(self):
        """Length alone does not make a question self-contained."""
        dependent, _ = followup.looks_dependent(
            "Does that apply to part-time students enrolled before 2019 as well?"
        )
        self.assertTrue(dependent)

    def test_self_contained_questions_are_left_alone(self):
        for question in (
            "What is the minimum attendance required for the examination?",
            "How many books may a student borrow from the library at once?",
            "What is the hostel fee for B.Tech MECH students this year?",
        ):
            with self.subTest(question=question):
                dependent, _ = followup.looks_dependent(question)
                self.assertFalse(dependent, f"{question!r} should stand alone")


class ContextualizeTests(SimpleTestCase):
    HISTORY = history(
        (
            "What is the hostel fee for B.Tech CSE?",
            "The hostel fee for B.Tech CSE is 60000 [1].",
        )
    )

    def test_a_follow_up_borrows_the_earlier_topic(self):
        """The case that abstained before this module existed."""
        resolved = followup.contextualize("What about MECH?", self.HISTORY)

        self.assertTrue(resolved.rewritten)
        self.assertEqual(resolved.original, "What about MECH?")
        # The user's own words stay first and intact.
        self.assertTrue(resolved.question.startswith("What about MECH?"))
        for term in ("hostel", "fee", "tech", "cse"):
            self.assertIn(term, resolved.question.lower())

    def test_the_users_own_words_are_never_replaced(self):
        resolved = followup.contextualize("And for ECE?", self.HISTORY)
        self.assertIn("ECE", resolved.question)
        self.assertEqual(resolved.original, "And for ECE?")

    def test_an_independent_question_is_returned_byte_for_byte(self):
        """Rewriting a good question corrupts retrieval for a turn that
        would otherwise have worked - the more damaging of the two errors."""
        question = "How many books may a student borrow from the library?"
        resolved = followup.contextualize(question, self.HISTORY)

        self.assertFalse(resolved.rewritten)
        self.assertEqual(resolved.question, question)
        self.assertEqual(resolved.borrowed_terms, ())

    def test_only_the_users_own_turns_contribute_terms(self):
        """An assistant turn is document content plus this system's prose.
        Feeding it back would make each turn drift toward whatever the last
        answer happened to quote."""
        turns = history(
            ("What is the hostel fee for CSE?", "Hostel fee is 60000 rupees [1].")
        )
        resolved = followup.contextualize("What about MECH?", turns)

        self.assertNotIn("60000", resolved.question)
        self.assertNotIn("rupees", resolved.question)

    def test_the_first_question_in_a_conversation_is_not_rewritten(self):
        resolved = followup.contextualize("What about MECH?", [])
        self.assertFalse(resolved.rewritten)
        self.assertEqual(resolved.question, "What about MECH?")

    def test_a_repeated_question_does_not_borrow_from_itself(self):
        turns = history(("What about MECH?", "I couldn't find that."))
        resolved = followup.contextualize("What about MECH?", turns)
        self.assertFalse(resolved.rewritten)

    @override_settings(RAG_FOLLOWUP_HISTORY_TURNS=1)
    def test_history_depth_is_bounded(self):
        """Stale terms are noise that actively degrades retrieval."""
        turns = history(
            ("What is the library fine for an overdue book?", "Ten rupees a day."),
            ("What is the hostel fee for B.Tech CSE?", "60000."),
        )
        resolved = followup.contextualize("What about MECH?", turns)

        self.assertIn("hostel", resolved.question.lower())
        self.assertNotIn("library", resolved.question.lower())

    @override_settings(RAG_FOLLOWUP_HISTORY_TURNS=0)
    def test_zero_history_turns_disables_resolution(self):
        """0 means one-shot questions. `turns[-0:]` is the whole list, so a
        naive slice would do the opposite of what the setting promises."""
        turns = history(
            ("What is the library fine for an overdue book?", "Ten rupees a day."),
            ("What is the hostel fee for B.Tech CSE?", "60000."),
        )
        resolved = followup.contextualize("What about MECH?", turns)

        self.assertFalse(resolved.rewritten)
        self.assertEqual(resolved.question, "What about MECH?")

    def test_an_empty_question_is_left_alone(self):
        resolved = followup.contextualize("", self.HISTORY)
        self.assertFalse(resolved.rewritten)
        self.assertEqual(resolved.question, "")

    def test_metadata_records_what_was_done_and_why(self):
        """A wrong answer must be traceable to the rewrite that caused it."""
        resolved = followup.contextualize("What about MECH?", self.HISTORY)
        meta = resolved.as_metadata

        self.assertTrue(meta["rewritten"])
        self.assertEqual(meta["original_question"], "What about MECH?")
        self.assertTrue(meta["resolved_question"])
        self.assertTrue(meta["reason"])
        self.assertTrue(meta["borrowed_terms"])

    def test_metadata_is_quiet_when_nothing_was_rewritten(self):
        resolved = followup.contextualize(
            "How many books may a student borrow from the library?", self.HISTORY
        )
        meta = resolved.as_metadata

        self.assertFalse(meta["rewritten"])
        self.assertEqual(meta["original_question"], "")
        self.assertEqual(meta["resolved_question"], "")
