"""The earlier turns of a conversation, as the answering model is shown them.

`followup.py` makes a follow-up RETRIEVABLE: "What about MECH?" is searched
with the terms of the question before it. That gets the right passages in
front of the model, and then the model was handed them with nothing else -
no earlier question, no earlier answer. It could not tell what "it", "that"
or "the second one" meant, what the user had already said about themselves
("I have 70 percent attendance"), or that it had just answered the same
thing for CSE. This module is the other half: the last few turns, put in the
prompt beside the passages.

WHAT THE HISTORY IS NOT is the part that matters. It is not a source. Every
fact in the answer still has to be in a passage retrieved, permission-filtered
and cited for THIS turn, and grounding checks it exactly as before: an earlier
answer's figure that this turn's passages do not state is rejected like any
other. The prompt says so, and the check does not take the prompt's word for
it. The one thing earlier turns add to the check is the user's own words
(engine: BuiltContext.user_text), so a figure the user gave about their OWN
situation two turns ago may be said back to them; facts.user_stated_clauses
decides what counts, and it never counts a figure they asked about.

WHAT IS SHOWN, and why each rule exists:

- The last RAG_ANSWER_HISTORY_MESSAGES messages (default 4: two exchanges),
  within RAG_ANSWER_HISTORY_CHARS characters (default 2400, about 600
  tokens) and MESSAGE_CHARS per message. Free tiers count tokens per minute
  (Groq: 8K, including the reply a request asks for), so the history is
  small, and the newest turns win: when the budget runs out the OLDER turns
  go, never one in the middle, which would show the model a conversation
  that did not happen.
- The conversation starts with a question. A window that opened on an
  answer whose question was cut off shows the model a reply to nothing.
- Citation markers are taken out of earlier answers. "[1]" in the last
  answer named a passage of the last context; in this prompt [1] is a
  different passage, and a model that copied the old marker would cite the
  wrong evidence - or, if it happened to be right, pass by luck.
- An earlier answer that was a refusal is shown as NOT_ANSWERED, not as the
  refusal sentence. The exact sentence the prompt tells the model to say
  when it finds nothing, repeated in the prompt as its own previous reply,
  is a reply to copy.
- Only what the model can use: role and text. Citations, notes and
  metadata stay in the database (the chat view does not even load them).

WHERE IT GOES: in the rendered prompt (context.render), not as extra chat
messages. One user message is what every provider here is sent, the
corrective retry reuses the prompt as it is, and "what the model saw is
reproducible" stays true of one string. Earlier answers sent as assistant
messages would also be the model's OWN words to continue, markers and all.

ONLY WHILE A MODEL WRITES THE ANSWER. A quote is chosen by matching the
question against passages; the conversation changes nothing about it, so a
quoting answerer gets the prompt it always got.
"""

import re
from dataclasses import dataclass

from django.conf import settings

from .llm import is_abstention

# Two exchanges. Enough for "and for ECE?" after "what about MECH?" after the
# original question, and for "the second one" to find the list it refers to.
DEFAULT_MESSAGES = 4

# About 600 tokens of history on top of a 2,000-5,000 token grounded prompt.
DEFAULT_CHARS = 2400

# One answer may be long (a list, a table read out). Its first part is what a
# follow-up usually points at, and a single long answer must not crowd the
# question it answered out of the budget.
MESSAGE_CHARS = 1000

# Shown in place of an earlier refusal; see the module docstring.
NOT_ANSWERED = "(no answer was found in the documents)"

# Message.Role values. Compared as strings, as followup.py does, so anything
# with .role and .content - a Message, a test double - can be passed in.
USER = "USER"
ASSISTANT = "ASSISTANT"

# A citation as stored on an answer: "[3]", and the grouped forms a model
# writes ("[1, 2]", "[1-2]") in case one reached the database unsplit. Any
# space before it goes too, so "per year [1]." reads "per year.".
_MARKERS = re.compile(r"\s*\[\d{1,3}(?:\s*[,\-–]\s*\d{1,3})*\]")


@dataclass(frozen=True)
class Turn:
    """One earlier message, as the model is shown it."""

    role: str  # USER or ASSISTANT
    text: str


def _clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word where there is one."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    words = head.rsplit(None, 1)
    cut = words[0] if len(words) > 1 else head
    return cut.rstrip(" ,;:") + " ..."


def _turn(message, limit: int):
    """`message` as a Turn, or None for one with nothing to show."""
    role = getattr(message, "role", "")
    text = (getattr(message, "content", "") or "").strip()
    if not text:
        return None
    if role == USER:
        return Turn(USER, _clip(text, limit))
    if role != ASSISTANT:
        return None
    if getattr(message, "abstained", False) is True or is_abstention(text):
        return Turn(ASSISTANT, NOT_ANSWERED)
    text = _MARKERS.sub("", text).strip()
    return Turn(ASSISTANT, _clip(text, limit)) if text else None


def recent_turns(messages, *, limit: int = None, budget: int = None) -> tuple:
    """The earlier turns to show the answering model, oldest first.

    `messages` is the conversation so far, oldest first - the Message
    queryset, or anything shaped like it - WITHOUT the question being
    answered now (the chat view saves it only after the answer).
    """
    limit = (
        limit
        if limit is not None
        else getattr(settings, "RAG_ANSWER_HISTORY_MESSAGES", DEFAULT_MESSAGES)
    )
    budget = (
        budget
        if budget is not None
        else getattr(settings, "RAG_ANSWER_HISTORY_CHARS", DEFAULT_CHARS)
    )
    # 0 switches the feature off, and has to be tested for: the slice below is
    # `[-limit:]`, and `[-0:]` is the WHOLE list (see followup.contextualize).
    if not messages or limit <= 0 or budget <= 0:
        return ()

    per_message = min(MESSAGE_CHARS, budget)
    chosen = []
    used = 0
    # Newest first, so it is the oldest turns the budget leaves out.
    for message in reversed(list(messages)[-limit:]):
        turn = _turn(message, per_message)
        if turn is None:
            continue
        if used + len(turn.text) > budget:
            break
        chosen.append(turn)
        used += len(turn.text)

    chosen.reverse()
    # A reply whose question did not fit is a reply to nothing.
    while chosen and chosen[0].role != USER:
        chosen.pop(0)
    return tuple(chosen)


def user_words(turns) -> list:
    """What the user typed in `turns`, oldest first.

    For grounding's user_text: a figure in here that states the user's own
    situation may be repeated by the answer. One entry per message, so the
    caller keeps them apart with a line break and a pronoun in one turn is
    never read as part of a clause from another.
    """
    return [turn.text for turn in turns if turn.role == USER]
