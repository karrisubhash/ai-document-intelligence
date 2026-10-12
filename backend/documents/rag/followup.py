"""Turning a follow-up into a question that can be retrieved on.

A conversation is not a list of independent questions, and until now this
system treated it as one. The chat endpoint stored every turn and read none
of them back: `agents.answer()` received the raw string the user typed, so

    "What is the hostel fee for B.Tech CSE?"      -> answered
    "What about MECH?"                            -> abstained

The second question is perfectly clear to a human and almost empty to a
retriever. "What about MECH?" carries one content term. Hybrid search over
twelve thousand documents will find whatever happens to mention "MECH",
rerank a handful of coincidences, and either abstain or answer from the
wrong document - and abstaining is the better of those two outcomes.

This module puts the missing information back before retrieval runs. It is
NOT conversation memory in the sense of feeding history to the model: the
answer is still built only from passages retrieved for this turn, still
permission-filtered, still grounded and still cited. What changes is only
the string retrieval searches on.

TWO PROPERTIES THIS HAS TO HAVE, because getting either wrong is worse than
not doing it at all:

*It must not touch a question that stands on its own.* Most turns are
self-contained, and appending the previous topic to "What is the library
fine policy?" would drag the last question's vocabulary into this one's
retrieval and pull the wrong documents up the ranking. So dependence is
detected first, and a question that looks independent is returned
unchanged, byte for byte.

*It must not invent.* The rewrite may only reuse terms the user themselves
wrote earlier in their own conversation. It never consults a document, so
it cannot leak one - and since retrieval re-runs its permission filter on
the rewritten question anyway, a rewrite could not widen access even if it
tried.

WHY A HEURISTIC AND NOT A MODEL. An LLM rewrite is better English and this
is not an English task - retrieval needs the right terms present, not a
grammatical sentence. "What about MECH? hostel fee B.Tech" is an ugly
string that retrieves exactly the right table row. A model-backed rewriter
fits behind the same function and is worth adding when there is a budget
for a second call per turn; the heuristic costs nothing, never times out,
and never hallucinates a topic the user did not raise.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings

from .reranking import STOPWORDS, TERM

# How many earlier turns may contribute vocabulary. Two user turns back is
# enough for "and for MECH?" following "what about CSE?" following the
# original question; further back and the topic has usually moved on, and
# stale terms are noise that actively degrades retrieval.
DEFAULT_HISTORY_TURNS = 2

# A question with no referring word (below) and at most this many content
# terms is treated as dependent. Elliptical follow-ups ("what about MECH?",
# "and for ECE?", "is that per semester?") carry one or two content terms
# once stopwords are removed - but the two-term ones all open with words
# REFERRING already catches, so the length rule only ever added ORDINARY
# short questions. At 2, "What is the exam fee?" after a hostel-fee question
# was rewritten to "...exam fee? hostel tech cse"; the hostel row then
# outranked the exam-fee row and the answer quoted both.
#
# An earlier draft set this at 5. Wrongly rewriting an independent question
# is the more damaging error of the two, because it corrupts a turn that
# would otherwise have worked and does not look like a failure afterwards.
# So the bar is a single term ("Weekends?").
MAX_DEPENDENT_TERMS = 1

# Openings and words that refer to something already said. A question
# containing one of these is dependent however long it is - "Does that
# apply to part-time students enrolled before 2019 as well?" is eleven
# words, carries five content terms, and means nothing on its own.
#
# Anaphora only. "There" is deliberately absent: "Is there a late fee?"
# opens an independent question far more often than it refers back to one,
# and a referring word that fires on ordinary questions is worse than a
# missing one.
REFERRING = re.compile(
    r"""
    ^\s*(?:and|but|so|also|then)\b          # continues a previous turn
  | ^\s*what\s+about\b
  | ^\s*how\s+about\b
  | ^\s*what\s+if\b
    # "Does that apply...", "Is it included...", "Can those be..." - but
    # not "Can IT reset...": capital IT is the department (see PRONOUN).
  | ^\s*(?:does|do|did|is|are|was|were|can|could|would|will|shall
        |should|has|have|had|must)\s+
      (?:that|this|(?-i:[Ii]t)|they|those|these|he|she)\b
  | \bthat\s+(?:one|policy|rule|figure|amount|document|case)\b
  | \bas\s+well\b                           # "...as well?" continues a turn
  | ^\s*why\b(?!\s+(?:is|are|do|does)\s+\w+\s+\w+)   # bare "why?"
    """,
    re.I | re.X,
)

# Pronouns refer back only when nothing earlier in the question could be
# their antecedent: "When is it due?" needs the conversation; "How do
# students renew their library cards?" does not - "their" is the students.
# Matched anywhere, the second was rewritten with the previous turn's
# vocabulary. And "IT" in capitals is the department, not the pronoun: "the
# budget of the IT department" referred to nothing.
PRONOUN = re.compile(
    r"(?-i:\b(?:[Ii]t|[Ii]ts)\b)"
    r"|\b(?:they|them|their|those|these|he|she|his|her|(?:the\s+)?same)\b",
    re.I,
)


@dataclass
class Contextualized:
    """What retrieval should search on, and why it differs from what was asked."""

    question: str
    original: str
    rewritten: bool = False
    reason: str = ""
    borrowed_terms: tuple = field(default_factory=tuple)

    @property
    def as_metadata(self) -> dict:
        """The shape stored on the assistant turn, for auditing a bad answer."""
        return {
            "rewritten": self.rewritten,
            "original_question": self.original if self.rewritten else "",
            "resolved_question": self.question if self.rewritten else "",
            "reason": self.reason,
            "borrowed_terms": list(self.borrowed_terms),
        }


# Greetings, thanks and other purely conversational turns. They have no
# content terms, so looks_dependent() used to read "hi!" as a follow-up and
# rewrite it into the previous question - and the user's greeting was
# answered with the attendance policy again (found live, 2026-10-11).
#
# No phrase may also be spelled as a shorter phrase plus a tail word. A
# separate "thank you" (also "thank" + "you") gave every repetition two
# parses, and a failing match tried all of them: 200 characters of
# "thank you thank you ... x" held a worker for over a second, and each
# further repetition doubled it.
_SMALL_TALK_PHRASE = (
    r"(?:h+i+|hel+o+|he+y+|hiya|yo|howdy|greetings"
    r"|good (?:morning|afternoon|evening|night|day)"
    r"|thanks?|thx|ty|cheers|ok(?:ay)?|cool|great|nice|awesome|perfect"
    r"|got it|bye|goodbye|see you|how are you(?: doing)?|how's it going"
    r"|who are you|what are you|what can you do)"
)
_SMALL_TALK_TAIL = (
    r"(?:there|again|so much|a lot|very much|you|bot|assistant|today|all)"
)
_SMALL_TALK = re.compile(
    rf"^{_SMALL_TALK_PHRASE}(?: (?:{_SMALL_TALK_PHRASE}|{_SMALL_TALK_TAIL}))*$"
)
# Small talk is short. Anything longer is not matched at all, which also
# bounds the regex's work whatever ambiguity a future phrase brings in.
_SMALL_TALK_MAX_WORDS = 10


def is_small_talk(question: str) -> bool:
    """A greeting, thanks or similar turn - not a question about documents."""
    words = re.sub(r"[^\w\s']", " ", (question or "").lower()).split()
    if not words or len(words) > _SMALL_TALK_MAX_WORDS:
        return False
    return bool(_SMALL_TALK.match(" ".join(words)))


def _content_terms(text: str) -> list:
    """Content words, in order, without stemming.

    Unstemmed on purpose: these terms are going back into a query string
    for the retriever to tokenize itself, and handing it a stem ("attend")
    instead of a word ("attendance") would weaken the lexical half of
    hybrid search - tsvector does its own normalisation and does it better.
    """
    return [
        term
        for term in TERM.findall(text.lower())
        if len(term) > 1 and term not in STOPWORDS
    ]


def looks_dependent(question: str) -> tuple:
    """Whether this question needs the conversation to make sense.

    Returns (dependent, reason). The reason is kept because a rewrite that
    turns out to have been wrong should be explainable after the fact.
    """
    stripped = (question or "").strip()
    if not stripped or is_small_talk(stripped):
        return False, ""

    if REFERRING.search(stripped):
        return True, "refers to something said earlier"

    # Two content words before the pronoun are taken to include its
    # antecedent ("students renew THEIR"); one is usually just a verb or an
    # adverb ("Can I get IT renewed?", "How long is IT valid?").
    for match in PRONOUN.finditer(stripped):
        if len(_content_terms(stripped[: match.start()])) < 2:
            return True, "refers to something said earlier"

    terms = _content_terms(stripped)
    if len(terms) <= MAX_DEPENDENT_TERMS:
        return True, f"only {len(terms)} content term(s) to retrieve on"

    return False, ""


def contextualize(
    question: str, history=None, *, max_turns: int = None
) -> Contextualized:
    """Resolve a follow-up against earlier turns in the same conversation.

    `history` is the conversation's turns, oldest first, as objects with
    `.role` and `.content` - the Message queryset, or anything shaped like
    it. Only the user's own earlier questions contribute terms: an
    assistant turn is document content plus this system's prose, and
    feeding that back into retrieval would make each turn drift toward
    whatever the last answer happened to quote.
    """
    original = (question or "").strip()

    if not original or not history:
        return Contextualized(question=original, original=original)

    dependent, reason = looks_dependent(original)
    if not dependent:
        return Contextualized(question=original, original=original)

    limit = (
        max_turns
        if max_turns is not None
        else getattr(settings, "RAG_FOLLOWUP_HISTORY_TURNS", DEFAULT_HISTORY_TURNS)
    )

    # 0 is documented as "one-shot questions", and it has to be tested for
    # explicitly: the slice below is `[-limit:]`, and `[-0:]` is the WHOLE
    # list. Without this, the setting that promises to switch the behaviour
    # off would instead borrow from every earlier turn in the conversation.
    if limit <= 0:
        return Contextualized(question=original, original=original)

    earlier = [
        message.content
        for message in history
        if getattr(message, "role", "") == "USER"
        and (message.content or "").strip() != original
    ][-limit:]

    if not earlier:
        return Contextualized(
            question=original,
            original=original,
            reason="dependent, but no earlier question to resolve against",
        )

    already = set(_content_terms(original))

    # Most recent first: when the topic has shifted, the nearest turn is
    # the one that shifted it, and its terms should win the budget.
    borrowed = []
    for content in reversed(earlier):
        for term in _content_terms(content):
            if term not in already and term not in borrowed:
                borrowed.append(term)

    if not borrowed:
        return Contextualized(
            question=original,
            original=original,
            reason="dependent, but earlier turns added no new terms",
        )

    # Appended rather than woven in. The user's own words stay first and
    # intact, so the reranker still scores against what they actually
    # asked, and the borrowed terms widen retrieval behind it. Producing a
    # grammatical sentence would be prettier and would not retrieve better.
    resolved = f"{original} {' '.join(borrowed)}"

    return Contextualized(
        question=resolved,
        original=original,
        rewritten=True,
        reason=reason,
        borrowed_terms=tuple(borrowed),
    )
