"""Query planning: splitting a question that asks for several things.

Pure functions over a string. No Django, no database, no model - the whole
plan is derived from the question text, so it can be read, tested and
argued with.

*This exists because it was measured, not because agents are fashionable.*
§22 is explicit that scaling and architecture decisions follow measured
workload rather than technology fashion, and a "planning agent" is exactly
the kind of component that gets added because it sounds advanced. So it was
measured first, against a five-document corpus containing every answer:

    "What is the hostel curfew and what is the tuition for CSE?"
        -> 2 of 2 parts answered. Single-shot retrieval was already fine.

    "What is the attendance minimum, how many books can a student borrow,
     and what is the hostel curfew?"
        -> 1 of 3.

    "What is the attendance minimum, what is the late return fine, what is
     the hostel curfew, and what does the bus pass cost?"
        -> 0 of 4. A flat abstention, with all four answers in the corpus.

The four-part case is the one that justifies this module. The answerer
scores a sentence by what share of the QUESTION's terms it covers, so every
part added to a question raises the bar for every sentence that answers any
one of them. Past three parts nothing clears it, and the system reports
that it could not find information it holds.

Two-part questions are left alone for the same reason: they already work,
and splitting them would buy a second retrieval for nothing.
"""

import re

# Beyond this, the question is not planned - it is run whole, and the
# answerer does what it can. A bound rather than a loop: each step costs a
# full retrieval, and an unbounded planner turns one expensive question
# into an unbounded number of them.
MAX_STEPS = 4

# A part shorter than this is a fragment - "and the fee", "or later" - not
# a question worth its own retrieval.
MIN_PART_CHARS = 12

# The interrogative that opens a question, captured so it can be carried
# onto a later part that does not repeat it: "What is the curfew and the
# tuition" -> the second part becomes "What is the tuition".
LEAD = re.compile(
    r"^\s*(who|what|when|where|which|why|how|is|are|does|do|can|may|must|should)\b"
    r"(\s+\w+){0,2}?\s+(is|are|was|were|can|may|does|do|must|should)?\s*",
    re.I,
)

# Words that make a fragment into a question of its own.
INTERROGATIVES = (
    "who",
    "what",
    "when",
    "where",
    "which",
    "why",
    "how",
    "is",
    "are",
    "does",
    "do",
    "can",
    "may",
    "must",
    "should",
    "list",
    "give",
    "show",
    "tell",
)

# Every boundary in one alternation, longest first so ", and " wins over
# both ", " and " and " at the same position.
#
# Tried as ONE split rather than as a list of strategies in order. The
# first version took whichever splitter matched first, and on
#
#   "What is the attendance minimum, how many books can a student borrow,
#    and what is the hostel curfew?"
#
# ", and " matched, produced two parts, and won - so the plain comma
# earlier in the sentence was never considered and a three-part question
# was planned as two. Being two parts, it was then left unplanned
# altogether, and the measured failure this module exists for went
# unfixed while every test still passed.
#
# These boundaries are ambiguous by nature ("the fine for late return and
# damage" is one question), which is why every candidate part is checked
# below before a split is accepted at all.
SPLITTER = re.compile(r"\?\s+|\s*;\s*|,\s+and\s+|,\s+|\s+and\s+", re.I)


def _looks_like_a_question(part: str) -> bool:
    words = part.strip().lower().split()
    if len(words) < 2:
        return False
    return words[0].strip("?,.") in INTERROGATIVES


def _lead_of(question: str) -> str:
    """The opening interrogative phrase, e.g. "What is"."""
    match = LEAD.match(question)
    return match.group(0).strip() if match else ""


def _clean(part: str) -> str:
    part = part.strip().strip(",;").strip()
    if part and not part.endswith("?"):
        part += "?"
    return part


def plan(question: str, *, max_steps: int = MAX_STEPS) -> list:
    """Split `question` into sub-questions, or return it unchanged.

    Returning `[question]` is the normal outcome and is not a failure: a
    question that asks one thing is a plan with one step, and the caller
    runs exactly the retrieval it would have run anyway.
    """
    question = (question or "").strip()
    if not question or max_steps <= 1:
        return [question] if question else []

    parts = _split(question, max_steps)

    # Two parts already work without planning - measured - so splitting
    # them would spend a second retrieval to reach the same answer.
    if len(parts) < 3:
        return [question]

    return parts[:max_steps]


def _split(question: str, max_steps: int) -> list:
    pieces = [piece for piece in SPLITTER.split(question) if piece and piece.strip()]
    if len(pieces) < 2 or len(pieces) > max_steps:
        return [question]

    lead = _lead_of(question)
    candidates = []

    for index, piece in enumerate(pieces):
        piece = piece.strip()
        # A later part that dropped the interrogative gets the opening one
        # back: "What is the curfew and the tuition" makes the second
        # retrieval "What is the tuition" rather than a bare noun phrase,
        # which retrieves far worse.
        if index and lead and not _looks_like_a_question(piece):
            piece = f"{lead} {piece}"
        candidates.append(_clean(piece))

    # Every part has to stand on its own as a question. One fragment means
    # the split was wrong, and a wrong split is worse than no split: it
    # spends a retrieval on nonsense and reports the part as unanswered.
    if all(
        len(part) >= MIN_PART_CHARS and _looks_like_a_question(part)
        for part in candidates
    ):
        return candidates

    return [question]
