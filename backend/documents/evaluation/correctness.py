"""Does an answer contain what the benchmark says the answer is?

The metrics this project already reported - groundedness, citation accuracy,
token overlap - say whether an answer was SUPPORTED, not whether it was RIGHT:
a fluent answer built from the right vocabulary scores well on overlap, and a
grounded answer to the wrong question scores well on groundedness. Without a
number for "right", an improvement to answers could not be told from a change
that merely made them longer.

This is a heuristic and says so. An answer is correct when it contains the key
facts of the expected answer:

- every FIGURE the expected answer states (a number, an amount, a date, a
  year range, a clock time), in any spelling that is the SAME figure:
  "75 percent" is "75%", "four books" is "4 books", "31 March 2025" is
  "31.03.2025", "9.15 am" is "09:15", "Rs 5/day per book" is "5 rupees per day
  per book", "a 12-character password" is "12 characters", "82,000.00" is
  "82000", "2024/25" is "2024-25", "65-75%" is "between 65 and 75", and a bare
  expected "four" is "4";
- and, when the expected answer states no figure at all, at least
  CONTENT_RECALL of its content words, since there is nothing exact to look
  for.

Three guards keep a number from rewarding the wrong thing:

- The QUESTION is not an answer. Its own text is taken out of the answer
  first, and an answer that only repeats the question's words is not credited
  with the figure the question itself states (a question's "sixty-five" would
  otherwise satisfy an expected "65").
- A pasted wall of text is not an answer. More than MAX_UNRELATED_TERMS (plus
  a share of the expected answer's own length) content words beyond those of
  the expected answer is a document, not an answer, however many of the right
  words it holds. An answer with helpful context around the fact is well
  inside that.
- A figure the expected answer gives a unit ("30 days", "200 rupees") is not
  met by the same number with only another unit ("30 months", "200 pages"),
  and a clock time keeps its am or pm ("11 pm" is not "11 am").

What it does not do, and why a correctness figure is a ceiling, not a
measurement:

- a figure is not tied to the entity it belongs to: "85000" is found whether
  it is said of B.Tech CSE or of the hostel, so a short answer with the right
  figure for the wrong programme counts;
- an answer that holds the right figure and little else, such as a whole fee
  table inside the length cap, counts even when it never says which row;
- it does not penalise an answer for ALSO saying something wrong, and it
  cannot read a negation ("the fee is NOT 5000");
- units are compared only where one is written right after the number
  (days, weeks, months, years, hours, minutes, semesters, pages, books,
  rupees, characters, credits), so "5 rupees per month" still meets "5 rupees
  per day"; and spellings it does not know (a day and month with no year as "15/08", a number written as
  a word inside a longer answer where nothing is counted) are rejected, so a
  model run can read lower than it is.

A human or a model reading the answers is the only real judge; this exists so
a change can be measured across dozens of questions without either.
"""

import re

from documents.rag import facts as fact_forms
from documents.rag import grounding

# Of the expected answer's content words, the share an answer must contain when
# the expected answer has no figure to look for. Lenient on purpose: a
# paraphrase is a correct answer.
CONTENT_RECALL = 0.6

# Content words an answer may hold beyond the expected answer's own, before it
# is a pasted document rather than an answer. Generous: a good answer gives
# the fact and the rule around it. The cap also grows with the expected
# answer, so a long expected answer is allowed a long answer.
MAX_UNRELATED_TERMS = 40
UNRELATED_PER_EXPECTED_TERM = 4

# An answer of fewer content words than this is never "just the question's
# words": "75 percent" is a fine answer to a question that says "percent".
RESTATEMENT_MIN_TERMS = 3

_UNIT = (
    "characters?|days?|weeks?|months?|years?|hours?|minutes?|pages?|books?"
    "|credits?|semesters?|terms?|marks?|percent|rupees?|digits?|words?"
)
_PER = "day|week|month|year|semester|hour|minute|book|page|copy|credit|term"
_COUNTED_UNIT = (
    r"(day|week|month|year|hour|minute|semester|page|book|rupee|character|credit)s?"
)

# "9.15 am", "9:15 am", "11 pm", "11 p.m.": a 12-hour clock.
_CLOCK_SUFFIXED = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I | re.ASCII
)
# "23:00", "09:15": a colon time needs no suffix to be a time.
_CLOCK_COLON = re.compile(r"(?<![\w:.])([01]?\d|2[0-3]):([0-5]\d)(?![\w:])", re.ASCII)
# "9.15" with no suffix is also a decimal number, so it is a time only when
# the expected answer states that very time.
_CLOCK_DOTTED = re.compile(r"(?<![\w:.])([01]?\d|2[0-3])\.([0-5]\d)(?![\w:]|\.\d)")
_CURRENCY = re.compile(r"(?:\b(?:rs|inr)\b\.?|₹)\s*(?=\d)", re.I | re.ASCII)
_PER_UNIT = re.compile(r"(\d)\s*/\s*(" + _PER + r")s?\b", re.I | re.ASCII)
_HYPHEN_UNIT = re.compile(r"(\d)-(?=(?:" + _UNIT + r")\b)", re.I | re.ASCII)
_ZERO_TAIL = re.compile(r"(?<=\d)\.00\b(?!\.\d)", re.ASCII)
_YEAR_SPAN = re.compile(
    r"(?<![\w\-/.])((?:19|20)\d{2})\s*[/\-–—]\s*((?:19|20)?\d{2})"
    r"(?!\w|[-/]\d)",
    re.ASCII,
)
_NUMBER_SPAN = re.compile(
    r"(?<![\w\-/.])(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)(?!\w|[-/.]\d)",
    re.ASCII,
)
_COUNTED_NUMBER = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:calendar |working |academic |full )?"
    + _COUNTED_UNIT
    + r"\b",
    re.I | re.ASCII,
)


def _clock(hour: int, minute: int) -> str:
    # One figure that nothing else can equal: no bare digits, and no
    # separator the fact reader splits on.
    return f" clock{hour:02d}_{minute:02d} "


def _suffixed_clock(match) -> str:
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not (1 <= hour <= 12) or minute > 59:
        return match.group(0)
    hour = hour % 12 + (12 if match.group(3).lower() == "p" else 0)
    return _clock(hour, minute)


def _year_span(match) -> str:
    # Only the separator changes; whether the two years follow each other is
    # decided where the fact is compared ("2024-26" is not "2024-25").
    return f"{match.group(1)}-{match.group(2)}"


def _number_span(match) -> str:
    first, second = match.group(1), match.group(2)
    # "2019-21" and "2019-2021" are spans of years, which the fact reader
    # already understands; "1000-2000" is two amounts.
    if re.fullmatch(r"(?:19|20)\d{2}", first) and (
        len(second) == 2 or re.fullmatch(r"(?:19|20)\d{2}", second)
    ):
        return match.group(0)
    return f"{first} to {second}"


def _normalise(text: str, clocks=frozenset()):
    """(the text with equivalent spellings in one form, the clock times it states)."""
    text = grounding.MARKER.sub(" ", fact_forms.normalize_markers(text or ""))
    text = _CURRENCY.sub("", text)
    text = _CLOCK_SUFFIXED.sub(_suffixed_clock, text)
    text = _CLOCK_COLON.sub(lambda m: _clock(int(m.group(1)), int(m.group(2))), text)
    stated = set(re.findall(r"clock\d{2}_\d{2}", text))

    def dotted(match):
        token = _clock(int(match.group(1)), int(match.group(2)))
        return token if token.strip() in clocks else match.group(0)

    text = _CLOCK_DOTTED.sub(dotted, text)
    text = _PER_UNIT.sub(r"\1 per \2", text)
    text = _HYPHEN_UNIT.sub(r"\1 ", text)
    text = _ZERO_TAIL.sub("", text)
    text = _YEAR_SPAN.sub(_year_span, text)
    text = _NUMBER_SPAN.sub(_number_span, text)
    return text, stated


def _bare_number_word(expected: str):
    """("four", "4") when the whole expected answer is one number word."""
    words = re.findall(r"[a-z]+", expected.lower())
    spelled = (getattr(fact_forms, "_UNITS", {}), getattr(fact_forms, "_TENS", {}))
    if not words or len(words) > 2:
        return None
    if not all(word in spelled[0] or word in spelled[1] for word in words):
        return None
    # The module reads a number word as a figure only beside what it counts.
    numerals = fact_forms.number_words(" ".join(words) + " days")
    if len(numerals) != 1:
        return None
    return " ".join(words), next(iter(numerals))


def expected_facts(expected: str) -> set:
    """The figures an answer must contain, as grounding compares them."""
    prepared = fact_forms.prepare(_normalise(expected)[0])
    stated = grounding._answer_facts(prepared)
    # "four books" states 4, which grounding only reads on the passage side.
    return stated | fact_forms.number_words(prepared)


def _without_question(answer: str, question: str) -> str:
    """The answer with the question's own words taken out, where it quotes them."""
    words = [word.strip("?.,;:!") for word in (question or "").split()]
    words = [word for word in words if word]
    if len(words) < 3:
        return answer
    pattern = r"\W*".join(re.escape(word) for word in words)
    return re.sub(pattern, " ", answer, flags=re.I)


def _unit_clash(expected_text: str, answer_text: str) -> bool:
    """The answer gives a required number only with a different unit."""
    for number, unit in _COUNTED_NUMBER.findall(expected_text):
        found = {
            found_unit.lower()
            for found_number, found_unit in _COUNTED_NUMBER.findall(answer_text)
            if found_number == number
        }
        if found and unit.lower() not in found:
            return True
    return False


def is_correct(answer: str, expected: str, question: str = "") -> bool:
    """Whether `answer` contains the key facts of `expected`.

    `question` is the question that was asked. It is optional so two strings
    can still be compared, but the evaluation runner always passes it.
    """
    if not (answer or "").strip() or not (expected or "").strip():
        return False

    answer = _without_question(answer, question)
    if not re.search(r"\w", answer):
        return False

    expected_text, clocks = _normalise(expected)
    answer_text, _ = _normalise(answer, clocks)
    required = expected_facts(expected)
    bare = None if required else _bare_number_word(expected)

    prepared = fact_forms.prepare(answer_text)
    present = grounding._support_facts(prepared)
    answer_terms = set(grounding._prose_terms(answer_text))
    expected_terms = set(grounding._prose_terms(expected_text))
    question_terms = set(grounding._prose_terms(_normalise(question)[0]))

    # Nothing but the question's own words.
    if (
        question_terms
        and len(answer_terms) >= RESTATEMENT_MIN_TERMS
        and answer_terms <= question_terms
    ):
        return False
    # The question states every figure it is being credited for.
    if (
        required
        and question_terms
        and required <= expected_facts(question)
        and answer_terms <= question_terms
    ):
        return False

    cap = MAX_UNRELATED_TERMS + UNRELATED_PER_EXPECTED_TERM * len(expected_terms)
    if len(answer_terms - expected_terms) > cap:
        return False

    if bare:
        word, numeral = bare
        return numeral in present or bool(
            re.search(r"\b" + re.escape(word) + r"\b", answer_text, re.I)
        )

    if required:
        cores = {grounding._numeric_core(fact) for fact in present} - {None}
        return all(
            grounding._fact_supported(fact, present, cores) for fact in required
        ) and not _unit_clash(expected_text, answer_text)

    if not expected_terms:
        return False
    return len(expected_terms & answer_terms) / len(expected_terms) >= CONTENT_RECALL
