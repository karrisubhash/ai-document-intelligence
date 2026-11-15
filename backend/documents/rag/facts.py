"""The same fact, written two ways.

Grounding refuses an answer that states a figure no cited passage contains,
and that is the guarantee this system exists for. But a check on TOKENS
rejects a correct answer for how it spells a fact:

    passage "31.03.2025"            answer "31 March 2025"
    passage "the 15th day of March, 2025"   answer "15 March 2025"
    passage "Fee Structure 2024-25" answer "the 2024-2025 fee"
    passage "within seven days"     answer "within 7 days"
    passage "7.30 pm"               answer "7:30 PM" (or "19:30")
    passage "Rs 3,00,000"           answer "Rs 3 lakh"
    passage "within 15 days"        answer "a 15-day window"
    answer  "[1, 2]", "[1-2]", "[Source 1]"  (citations, written another way)

Each of those threw away a whole, correct, cited answer and showed the reader
"I couldn't find enough information". The helpers here recognise those
spellings and reduce them to one form on both sides, and nothing wider. Every
widening is also a way to accept a WRONG figure, so each one is narrow, and an
EQUIVALENCE of two spellings of one fact - never "any figure that appears":

- Only dates that can be read one way. A month name or an ISO date always can.
  A numeric date can when one of its numbers is over 12 (31.03.2025) or both
  are equal. "04.03.2025" is the 4th of March or the 3rd of April, so it is
  left alone and must match as written - accepting either reading would accept
  a wrong date.
- A date is compared as a whole. Its day or its year never stands in for it,
  so "31 May 2025" is not supported by a passage that says "31 March 2025".
- A number word counts on the PASSAGE side only, and only when it counts
  something: "seven days" supports 7, "No one may attend" and "the second
  semester" support nothing. Without that, any passage containing "one" would
  have supported any answer that said 1. "One" is an idiom far more often than
  a quantity ("at one time", "one of", "one another", "one member"), so the bare
  word stands for 1 only before a unit of time ("within one week"); a
  compound with a scale word ("one hundred and eighty", "two thousand", "one
  lakh") is unambiguously a quantity, and so is a number word after "up to",
  "at least" or "no more than" ("may borrow up to six.").
- A range of years supports each of its years only when they are consecutive
  ("2024-25" is one academic year). "Rs 1000-2000" does not support "Rs 2000".
- A range "65-75" the answer states is supported by a passage that states the
  same range - "65-75", "65 - 75", "65 to 75", "between 65 and 75" - with both
  ends together in ONE passage within a short window. Its two ends appearing
  anywhere in the cited passages ("fail mark 40", "attendance 75") do not make
  "40-75" a fact.
- A time is the same time however it is written ("7:30 PM", "7.30 pm") and a
  24-hour time that cannot be read two ways ("17:00", "09:00") is the 12-hour
  time. "5:00" alone could be either and is not converted.
- "3 lakh" is 300000 and "1 crore" 10000000, exactly, in both directions.
- Invisible format characters (soft hyphens, zero-width spaces) are not part of
  a figure, and a comma between digits is a thousands separator only when the
  groups are well formed (85,000 or 1,00,000); "7,5" and "1,2,3" are separate
  numbers.
- A citation is "[1]", "[1, 2]", "[1-2]", "[ 1 ]", "[Source 1]", "[1 and 2]"
  or the same in full-width brackets, and "(1)" is not one. A group is split
  only when every number in it is a passage that exists: "[0, 1]" is an
  interval.
- Only ASCII is matched. Case-insensitive matching otherwise treats "\N{LATIN SMALL LETTER LONG S}"
  as an "s", and a month name that is not in the table must never be an error:
  an answer or a document that spells one oddly cannot be allowed to make the
  check itself fail.
"""

import datetime
import re
import unicodedata
from decimal import Decimal

MONTH_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
# Built from the table so the two cannot disagree: a regex that accepted a
# spelling the table lacked ("sepember") was a KeyError, and a KeyError in
# grounding is a failed request.
_MONTH = "(?:" + "|".join(sorted(MONTH_NUMBER, key=len, reverse=True)) + ")"

_FLAGS = re.I | re.ASCII

# The hyphens that are not the ASCII one: the hyphen, the non-breaking hyphen (what
# gpt-oss writes in "2024-25"), the figure dash, the minus sign and two
# compatibility forms. Built from code points so no editor or tool can turn them
# into the one they look like. The en and em dash are named in the patterns below.
_HYPHEN_CHARS = "".join(
    chr(code) for code in (0x2010, 0x2011, 0x2012, 0x2212, 0xFE63, 0xFF0D)
)

_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH + r")\b\.?,?\s+(\d{4})\b",
    _FLAGS,
)
_MONTH_DAY_YEAR = re.compile(
    r"\b(" + _MONTH + r")\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", _FLAGS
)
# "on the 15th day of March, 2025": formal regulations write it this way.
_DAY_OF_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+(" + _MONTH + r")\b\.?,?\s+(\d{4})\b",
    _FLAGS,
)
# "15 to 20 March 2025", "15-20 March 2025": two dates.
_DAY_RANGE_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:to|until|till|through|-|\N{EN DASH}|\N{EM DASH})"
    r"\s*(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH + r")\b\.?,?\s+(\d{4})\b",
    _FLAGS,
)
# "15 September" and "September 15" with no year. "May" is left out: "Section
# 15 may apply" is not a date, and a pattern that read it as one would reject
# answers for a figure the reader never saw as a date.
_MONTH_NO_MAY = (
    "(?:"
    + "|".join(sorted((m for m in MONTH_NUMBER if m != "may"), key=len, reverse=True))
    + ")"
)
_DAY_MONTH_NO_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_NO_MAY + r")\b", _FLAGS
)
_MONTH_DAY_NO_YEAR = re.compile(
    r"\b(" + _MONTH_NO_MAY + r")\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", _FLAGS
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b", re.ASCII)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", re.ASCII)

# "2024-25" and "2024-2025" are one academic year, and a range typed with an
# en dash ("2024\N{EN DASH}25") is the same range as one typed with a hyphen.
_YEAR_RANGE_DASH = re.compile(
    r"\b(\d{4})\s*[\N{EN DASH}\N{EM DASH}]\s*(\d{2}|\d{4})\b", re.ASCII
)
_SHORT_RANGE = re.compile(r"(\d{4})-(\d{2})", re.ASCII)
_LONG_RANGE = re.compile(r"(\d{4})-(\d{4})", re.ASCII)
_NUMBER_RANGE = re.compile(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", re.ASCII)

# --- citations, in the forms a model writes them ----------------------------

_OPEN = r"[\[\N{FULLWIDTH LEFT SQUARE BRACKET}\N{LEFT BLACK LENTICULAR BRACKET}]"
_CLOSE = r"[\]\N{FULLWIDTH RIGHT SQUARE BRACKET}\N{RIGHT BLACK LENTICULAR BRACKET}]"
# \s is ASCII-only under re.ASCII, and a model puts a no-break space where it
# writes one ("[1,<NARROW NO-BREAK SPACE>2]"): the space separators count too.
_WS = (
    r"[\s"
    + "".join(chr(code) for code in (0xA0, 0x1680, *range(0x2000, 0x200B), 0x202F))
    + "".join(chr(code) for code in (0x205F, 0x3000))
    + r"]*"
)
_REF = r"(?:(?:sources?|passages?|refs?\.?|references?)" + _WS + r")?\d{1,3}"
_REF_SEPARATOR = (
    _WS
    + r"(?:[,;&]|\band\b|\bto\b|[-\N{EN DASH}\N{EM DASH}"
    + _HYPHEN_CHARS
    + r"])"
    + _WS
)
# "[1]", "[ 1 ]", "[Source 1]", "[1, 2]", "[1; 2]", "[1 and 2]", "[1-2]",
# "[Source 1, Source 2]" and the same in full-width brackets. "(1)" is not
# here: a number in parentheses is far too often a number.
CITATION_GROUP = re.compile(
    _OPEN
    + _WS
    + r"\^?"
    + _WS
    + "("
    + _REF
    + r"(?:"
    + _REF_SEPARATOR
    + _REF
    + r")*)"
    + _WS
    + _CLOSE,
    _FLAGS,
)
# Kept for what imported it under its old name.
GROUPED_MARKER = CITATION_GROUP

# A range of markers is expanded; one that spans more than this is not a
# citation, it is a range of something else.
_MAX_MARKER_SPAN = 50

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {
    "thousand": 1000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
}
# What a number can count in an institution's documents. A number word counts
# only when one of these follows it (with at most one qualifier between:
# "ten working days"), because "one" and "second" turn up in nearly every
# passage and mean nothing as a figure.
_COUNTED = (
    "day|days|week|weeks|month|months|year|years|hour|hours|minute|minutes"
    "|semester|semesters|term|terms|credit|credits|mark|marks|book|books|copy"
    "|copies|attempt|attempts|time|times|percent|per\\s+cent|rupee|rupees"
    "|student|students|member|members|course|courses|subject|subjects|paper"
    "|papers|question|questions|page|pages|word|words|character|characters"
    "|installment|installments|instalment|instalments|session|sessions|class"
    "|classes|lecture|lectures|period|periods|batch|batches|seat|seats|room"
    "|rooms"
)
# The bare word "one" is a quantity before these, and an idiom before nearly
# anything else ("at one time", "one member", "one of").
_ONE_COUNTS = frozenset({"day", "week", "month", "year", "hour", "minute"})

_ATOM = (
    r"(?:(?:"
    + "|".join(_TENS)
    + r")(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|"
    + "|".join(sorted(_UNITS, key=len, reverse=True))
    + r")"
)
_BELOW_THOUSAND = (
    r"(?:" + _ATOM + r"\s+hundred(?:\s+(?:and\s+)?" + _ATOM + r")?|" + _ATOM + r")"
)
_PHRASE = (
    r"(?:"
    + _BELOW_THOUSAND
    + r"\s+(?:"
    + "|".join(sorted(_SCALES, key=len, reverse=True))
    + r")(?:\s+(?:and\s+)?"
    + _BELOW_THOUSAND
    + r")?|"
    + _BELOW_THOUSAND
    + r")"
)
# A number word after one of these is a quantity whatever follows it: "may borrow
# up to six." says how many, and nothing is left to count it ("up to six" is the
# clause's last words). Never "one": "at least one of the following" is an idiom.
_QUANTIFIER = (
    r"(?:up\s+to|upto|at\s+least|at\s+most|no\s+more\s+than|not\s+more\s+than"
    r"|more\s+than|fewer\s+than|less\s+than|maximum\s+of|minimum\s+of|exactly)"
)
_NUMBER_WORD = re.compile(
    r"\b("
    + _QUANTIFIER
    + r"\s+)?("
    + _PHRASE
    + r")\b(\s+(?:(?:calendar|working|academic|full|additional|consecutive)\s+)?("
    + _COUNTED
    + r")\b)?",
    _FLAGS,
)

# "3 lakh", "2.5 lakhs", "1 crore": amounts the way an Indian document writes
# them. The figure is bounded so the arithmetic below cannot be made to work hard.
_AMOUNT = re.compile(
    r"(?<![\d.])(\d{1,12}(?:\.\d{1,6})?)[ \t]{0,3}-?[ \t]{0,3}(lakhs?|lacs?|crores?)\b",
    _FLAGS,
)

# Clock times. With a meridiem ("7:30 PM", "7.30 pm", "7 p.m.") the hour and
# minute are unambiguous. Without one, only a 24-hour time that cannot be read
# as 12-hour: an hour over 12, midnight's "00", or a leading zero ("09:00").
_CLOCK_MERIDIEM = re.compile(
    r"(?<![\w.:])(\d{1,2})(?:[:.](\d{2}))?[ \t]*([ap])\.?m\b\.?", _FLAGS
)
_CLOCK_24 = re.compile(r"(?<![\w.:])(\d{1,2}):(\d{2})(?![\w:]|\.\d)", re.ASCII)

# Two figures are a range when they stand this close, with a range word or a
# dash between them: "between 65 and 75", "65 to 75", "65-75", "65 - 75".
RANGE_WINDOW = 40
_FIGURE = re.compile(r"\d+(?:\.\d+)?(?::\d{2})?", re.ASCII)
_RANGE_WORDS = frozenset({"to", "till", "until", "through", "thru", "upto"})
_NOT_A_RANGE_GAP = re.compile(r"[;|\n]|(?<![Rr][Ss])[.!?](?:\s|$)")
_BETWEEN = re.compile(r"between\W+(?:\w+\W+){0,2}$", re.I | re.ASCII)
_DASHES = "-\N{EN DASH}\N{EM DASH}"
_DASH_BETWEEN = r"[ \t]*[\-\N{EN DASH}\N{EM DASH}][ \t]*"
_DASHED_NUMBERS = re.compile(
    r"(?<![\d.])\d+(?:\.\d+)?(?:" + _DASH_BETWEEN + r"\d+(?:\.\d+)?)+", re.ASCII
)
_DASH_SPLIT = re.compile(_DASH_BETWEEN, re.ASCII)

_DIGIT_COMMAS = re.compile(r"(?<!\d)\d+(?:,\d+)+", re.ASCII)
# 85,000 and 1,234,567; and the Indian grouping 1,00,000 and 1,47,500.
_WELL_FORMED = re.compile(r"\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}", re.ASCII)


def strip_format(text: str) -> str:
    """Text without its invisible format characters (Unicode category Cf).

    A soft hyphen or a zero-width space inside a figure ("7<ZWSP>5") splits it
    into two figures that a passage may state separately, and shows the reader
    "75". Only for READING figures: what is shown to a reader is never altered
    (a zero-width joiner is part of how Indic scripts are spelled).
    """
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def lookalikes(text: str) -> str:
    """Text with every space separator read as a space, every hyphen-like character
    as a hyphen and every decimal digit as 0-9.

    Every pattern here looks for an ordinary space, hyphen and digit, and real text
    is not that tidy. gpt-oss writes "15<NARROW NO-BREAK SPACE>August" and
    "2024<NON-BREAKING HYPHEN>25" in most of its answers, and a Word or PDF
    document puts a no-break space between a number and its unit. A date whose
    space is not read is only the bare day, which "15 August" supports: "15
    September" was accepted. A range whose hyphen is not read is two figures nobody
    wrote: the right answer was refused. Only for READING figures, like
    strip_format: what a reader is shown is never altered. The en and em dash stay
    as they are, the range patterns name them.
    """
    if text.isascii():
        return text
    out = []
    for char in text:
        if char < chr(128):
            out.append(char)
        elif unicodedata.category(char) == "Zs":
            out.append(" ")
        elif char in _HYPHEN_CHARS:
            out.append("-")
        else:
            digit = unicodedata.decimal(char, None)
            out.append(char if digit is None else str(digit))
    return "".join(out)


def digit_commas(text: str) -> str:
    """A comma between digits, read as a thousands separator only when it is one.

    "85,000" and "1,00,000" are figures. "7,5" and "1,2,3" are a decimal comma
    and a list, and joined they were "75" and "123": numbers that no one wrote.
    """

    def read(match):
        run = match.group(0)
        if _WELL_FORMED.fullmatch(run):
            return run.replace(",", "")
        return run.replace(",", " ")

    return _DIGIT_COMMAS.sub(read, text)


def clean(text: str) -> str:
    """Text as figures are read from it: format characters gone, lookalike spaces,
    hyphens and digits written the ordinary way, commas read."""
    return digit_commas(lookalikes(strip_format(text)))


def prepare(text: str) -> str:
    """Text with year ranges written one way, before figures are read from it."""
    return _YEAR_RANGE_DASH.sub(r"\1-\2", clean(text))


def amounts(text: str) -> str:
    """Text with "3 lakh" and "1 crore" written as the number they are.

    Exact: "2.5 lakh" is 250000, "0.85 lakh" is 85000. Both sides are read this
    way, so "Rs 3 lakh" and "Rs 3,00,000" and "300000" are one amount.
    """

    def number(match):
        scale = 10_000_000 if match.group(2).lower().startswith("c") else 100_000
        return format((Decimal(match.group(1)) * scale).normalize(), "f")

    return _AMOUNT.sub(number, text)


def range_dashes(text: str) -> str:
    """A dash, spaced or not, hyphen or en or em dash, between two numbers as
    one plain hyphen: how an answer's range "65 - 75" or "65\N{EN DASH}75"
    becomes one figure "65-75" that has to be a range in a passage.

    Only a pair. Three numbers in a chain ("05 - 06 - 2025", "1-2-3") are a
    date or a part number, not a range, and stay as they are."""

    def pair(match):
        parts = _DASH_SPLIT.split(match.group(0))
        return "-".join(parts) if len(parts) == 2 else match.group(0)

    return _DASHED_NUMBERS.sub(pair, text)


def _marker_numbers(inner: str):
    """The markers a bracket names, a range expanded; None for one that is not."""
    tokens = re.findall(r"\d{1,3}|\bto\b|[" + _DASHES + r"]", inner, _FLAGS)
    numbers = []
    index = 0
    while index < len(tokens):
        if not tokens[index].isdigit():
            index += 1
        elif index + 2 < len(tokens) and tokens[index + 2].isdigit():
            first, last = int(tokens[index]), int(tokens[index + 2])
            if last < first or last - first >= _MAX_MARKER_SPAN:
                return None
            numbers.extend(range(first, last + 1))
            index += 3
        else:
            numbers.append(int(tokens[index]))
            index += 1
    return numbers or None


def normalize_markers(text: str, available=None) -> str:
    """Every citation written as "[1][2]", which is how one is recognised.

    "[1, 2]", "[1-2]", "[1 and 2]", "[Source 1, Source 2]", "[ 1 ]" and the
    full-width forms all name passages. With `available` (the markers the
    context has), a group is split only when EVERY number in it is one of them:
    "[0, 1]" and "[2, 12]" are an interval and a document's own reference, and
    reading them as citations withheld correct answers. A single number in a
    longer form ("[ 7 ]", "[Source 7]") is still a citation, of a passage that
    does not exist: the cheapest hallucination to catch stays caught.
    """

    def split(match):
        numbers = _marker_numbers(match.group(1))
        if numbers is None:
            return match.group(0)
        if len(numbers) == 1:
            return f"[{numbers[0]}]"
        if available is not None and not all(n in available for n in numbers):
            return match.group(0)
        return "".join(f"[{n}]" for n in numbers)

    return CITATION_GROUP.sub(split, text)


def _canonical(year: int, month, day: int):
    if month is None:
        return None
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return f"date:{year:04d}-{month:02d}-{day:02d}"


def _opaque(original: str) -> str:
    """A date that matched a date pattern and is not a real date.

    "31 April 2025" is kept as ONE token that has to appear exactly: split into
    a day and a year it was satisfied by any 31 and any 2025 in the passages,
    so a passage that says "31 March 2025" supported it.
    """
    return "date:?" + re.sub(r"[^a-z0-9]", "", original.lower())


def year_less(dates: set) -> set:
    """Each full date as its day and month alone, which is what an answer that
    leaves the year out states ("15 August" for "15 August 2025")."""
    return {
        "date:--" + date[len("date:2025-") :]
        for date in dates
        if re.fullmatch(r"date:\d{4}-\d{2}-\d{2}", date, re.ASCII)
    }


def extract_dates(text: str, *, inline: bool = False) -> tuple[str, set]:
    """(text with each date taken out, the dates as canonical tokens).

    A numeric date that can be read two ways stays in the text exactly as
    written. With `inline`, each date is replaced by one word that names it
    ("date20250331") instead of being taken out, which is what a word-overlap
    comparison needs: "31st March 2025" and "31.03.2025" share no words, and
    are one date.
    """
    found = set()

    def take(tokens, original):
        tokens = [token for token in tokens if token]
        if not tokens:
            return original
        found.update(tokens)
        if inline:
            return (
                " "
                + " ".join(token.replace(":", "").replace("-", "") for token in tokens)
                + " "
            )
        return " "

    def month_of(word):
        return MONTH_NUMBER.get(word.lower())

    def one(year, month, day, original, *, worded=False):
        token = _canonical(int(year), month, int(day))
        if token:
            return take([token], original)
        # Worded and not a real date: one exact token. A numeric or ISO one is
        # left in the text, where it is already a single token.
        return take([_opaque(original)], original) if worded else original

    def day_month_year(match):
        return one(
            match.group(3),
            month_of(match.group(2)),
            match.group(1),
            match.group(0),
            worded=True,
        )

    def month_day_year(match):
        return one(
            match.group(3),
            month_of(match.group(1)),
            match.group(2),
            match.group(0),
            worded=True,
        )

    def iso(match):
        return one(match.group(1), int(match.group(2)), match.group(3), match.group(0))

    def day_range(match):
        month = month_of(match.group(3))
        first = _canonical(int(match.group(4)), month, int(match.group(1)))
        last = _canonical(int(match.group(4)), month, int(match.group(2)))
        if not (first and last):
            return take([_opaque(match.group(0))], match.group(0))
        return take([first, last], match.group(0))

    def numeric(match):
        first, second, year = (int(match.group(n)) for n in (1, 2, 3))
        if first > 12 and second <= 12:
            day, month = first, second
        elif second > 12 and first <= 12:
            month, day = first, second
        elif first == second:
            day = month = first
        else:
            return match.group(0)  # 04.03.2025: the 4th of March or of April
        return one(year, month, day, match.group(0))

    def no_year(month_word, day, original):
        month = month_of(month_word)
        # 2000 is a leap year: only 30 February and the like are impossible.
        if _canonical(2000, month, int(day)):
            return take([f"date:--{month:02d}-{int(day):02d}"], original)
        return take([_opaque(original)], original)

    def day_month(match):
        return no_year(match.group(2), match.group(1), match.group(0))

    def month_day(match):
        return no_year(match.group(1), match.group(2), match.group(0))

    for pattern, replace in (
        (_ISO_DATE, iso),
        (_DAY_RANGE_MONTH_YEAR, day_range),
        (_DAY_OF_MONTH_YEAR, day_month_year),
        (_DAY_MONTH_YEAR, day_month_year),
        (_MONTH_DAY_YEAR, month_day_year),
        (_NUMERIC_DATE, numeric),
        (_DAY_MONTH_NO_YEAR, day_month),
        (_MONTH_DAY_NO_YEAR, month_day),
    ):
        text = pattern.sub(replace, text)
    return text, found


def extract_times(text: str) -> tuple[str, list]:
    """(text with each clock time taken out, [(the time, the figure as written)]).

    The time is "time:19:30" for "7:30 PM", "7.30 pm" and "19:30" alike. The
    figure as written ("7:30") is what an answer may fall back on when the
    passage gives the hour without its meridiem ("from 4 to 7 pm"). A time that
    could be 12-hour or 24-hour and carries no meridiem ("5:00") is left in the
    text as it is.
    """
    found = []

    def meridiem(match):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if not (1 <= hour <= 12 and minute <= 59):
            return match.group(0)
        afternoon = match.group(3).lower() == "p"
        hour24 = hour % 12 + (12 if afternoon else 0)
        written = re.match(r"\d{1,2}(?:[:.]\d{2})?", match.group(0)).group(0)
        found.append((f"time:{hour24:02d}:{minute:02d}", written))
        return " "

    def twenty_four(match):
        digits, hour, minute = match.group(1), int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return match.group(0)
        if not (hour >= 13 or hour == 0 or (len(digits) == 2 and digits[0] == "0")):
            return match.group(0)
        found.append((f"time:{hour:02d}:{minute:02d}", f"{digits}:{match.group(2)}"))
        return " "

    text = _CLOCK_MERIDIEM.sub(meridiem, text)
    text = _CLOCK_24.sub(twenty_four, text)
    return text, found


def _range_end(figure: str) -> str:
    """A range's end as it is compared: "9:00" is "9"."""
    return figure[: -len(":00")] if figure.endswith(":00") else figure


def range_pairs(text: str) -> set:
    """The ranges a text states, each as "range:65-75".

    Two figures are a range when they stand within RANGE_WINDOW characters of
    each other with nothing but a few words between them and a dash or a range
    word ("to", "until") among them, or "and" after a "between": "between 65
    and 75 percent", "from Rs 1500 to Rs 2000", "65 - 75". "Attendance 65 and
    a fee of 75" is two figures, and so is a table row.
    """
    pairs = set()
    figures = list(_FIGURE.finditer(text))
    for first, second in zip(figures, figures[1:], strict=False):
        gap = text[first.end() : second.start()]
        if len(gap) > RANGE_WINDOW or _NOT_A_RANGE_GAP.search(gap):
            continue
        words = re.findall(r"[a-z]+", gap.lower())
        if sum(1 for word in words if word not in _RANGE_WORDS and word != "and") > 3:
            continue
        if any(char in gap for char in _DASHES) or _RANGE_WORDS.intersection(words):
            joined = True
        elif "and" in words:
            before = text[max(0, first.start() - 30) : first.start()]
            joined = bool(_BETWEEN.search(before))
        else:
            joined = False
        if joined:
            pairs.add(
                f"range:{_range_end(first.group(0))}-{_range_end(second.group(0))}"
            )
    return pairs


_SHORT_RANGE_IN_TEXT = re.compile(r"\b(\d{4})-(\d{2})\b", re.ASCII)


def prose_form(text: str) -> str:
    """Text with dates and year ranges in one form, for the word-overlap check.

    That check compares words, so "31st March 2025" against "31.03.2025", or
    "2024-25" against "2024-2025", counted as unsupported words even when the
    figure check had accepted them.
    """
    text, _ = extract_dates(amounts(prepare(text)), inline=True)
    return _SHORT_RANGE_IN_TEXT.sub(lambda m: canonical_range(m.group(0)), text)


def canonical_range(token: str) -> str:
    """ "2024-25" as "2024-2025"; anything else as it came."""
    match = _SHORT_RANGE.fullmatch(token)
    if match:
        year = int(match.group(1))
        if int(match.group(2)) == (year + 1) % 100:
            return f"{year}-{year + 1}"
    return token


def range_years(fact: str) -> set:
    """The two years of an academic-year range such as "2024-2025".

    Only consecutive years, and only years: "Rs 1000-2000" is a range of
    amounts, and supporting "Rs 2000" from it would state a limit as a fee.
    """
    match = _LONG_RANGE.fullmatch(fact)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        if second == first + 1 and 1900 <= first <= 2100:
            return {match.group(1), match.group(2)}
    return set()


def range_parts(fact: str):
    """("2019", "2021") for the fact "2019-2021"; None for anything else."""
    match = _NUMBER_RANGE.fullmatch(fact)
    return match.groups() if match else None


def _phrase_value(words) -> int:
    """The number "one hundred and eighty" or "two thousand five hundred" is."""
    total = current = 0
    for word in words:
        if word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = max(current, 1) * 100
        elif word in _SCALES:
            total += max(current, 1) * _SCALES[word]
            current = 0
    return total + current


def number_words(text: str) -> set:
    """The numerals a passage's number words stand for, when they count something.

    "seven days" and "twenty-five rupees" as "7" and "25"; "one hundred and
    eighty", "two thousand" and "two lakh" as 180, 2000 and 200000, with or
    without a noun after them, because a scale word makes a quantity. For the
    passage side only, and never for "one" or an ordinal on its own (see the
    module docstring): "No one may attend", "at one time", "the second
    semester". The bare word "one" counts before a unit of time and nowhere else.
    After "up to", "at least", "no more than" and the like a number word is a
    quantity with nothing after it ("may borrow up to six.").
    """
    numerals = set()
    for match in _NUMBER_WORD.finditer(text):
        words = re.findall(r"[a-z]+", match.group(2).lower())
        noun = re.sub(r"\s+", " ", (match.group(4) or "").lower())
        scaled = any(word == "hundred" or word in _SCALES for word in words)
        if scaled:
            counts = True
        elif words == ["one"]:
            counts = noun in _ONE_COUNTS
        else:
            counts = bool(noun) or bool(match.group(1))
        if counts:
            numerals.add(str(_phrase_value(words)))
    return numerals


# --- what a reader and a model are told --------------------------------------

# The month as a name: the longest spelling the table has for each number.
_MONTH_NAME = {
    number: name.capitalize()
    for name, number in sorted(MONTH_NUMBER.items(), key=lambda item: len(item[0]))
}
_ORDINAL = r"(?:st|nd|rd|th)?"
_SPELLED_RANGE = re.compile(
    r"(\d{1,2})"
    + _ORDINAL
    + r"(to|until|till|through)(\d{1,2})"
    + _ORDINAL
    + r"(?:of)?("
    + _MONTH
    + r")(\d{4})"
)
_SPELLED_DAY_MONTH_YEAR = re.compile(
    r"(\d{1,2})" + _ORDINAL + r"(?:day)?(?:of)?(" + _MONTH + r")(\d{4})"
)
_SPELLED_MONTH_DAY_YEAR = re.compile(
    r"(" + _MONTH + r")(\d{1,2})" + _ORDINAL + r"(\d{4})"
)
_SPELLED_DAY_MONTH = re.compile(
    r"(\d{1,2})" + _ORDINAL + r"(?:day)?(?:of)?(" + _MONTH + r")"
)


def _month_name(word: str) -> str:
    return _MONTH_NAME[MONTH_NUMBER[word]]


def _readable_date(body: str) -> str:
    """A date token as a person writes the date."""
    if body.startswith("?"):
        spelled = body[1:]
        shown = None
        if match := _SPELLED_RANGE.fullmatch(spelled):
            first, word, last, month, year = match.groups()
            shown = f"{int(first)} {word} {int(last)} {_month_name(month)} {year}"
        elif match := _SPELLED_DAY_MONTH_YEAR.fullmatch(spelled):
            day, month, year = match.groups()
            shown = f"{int(day)} {_month_name(month)} {year}"
        elif match := _SPELLED_MONTH_DAY_YEAR.fullmatch(spelled):
            month, day, year = match.groups()
            shown = f"{int(day)} {_month_name(month)} {year}"
        elif match := _SPELLED_DAY_MONTH.fullmatch(spelled):
            day, month = match.groups()
            shown = f"{int(day)} {_month_name(month)}"
        if shown is None:
            shown = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", spelled)
        return f"{shown} (not a real date)"
    if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", body):
        year, month, day = match.groups()
        return f"{int(day)} {_MONTH_NAME[int(month)]} {year}"
    if match := re.fullmatch(r"--(\d{2})-(\d{2})", body):
        month, day = match.groups()
        return f"{int(day)} {_MONTH_NAME[int(month)]}"
    return body


def readable(fact: str) -> str:
    """A fact as a person reads it. The tokens grounding compares ("date:--08-15",
    "date:?31april2025", "time:19:30|7:30") are internal: shown to a reader, or
    to a model that is asked to correct itself, they say nothing."""
    try:
        if fact.startswith("date:"):
            return _readable_date(fact[len("date:") :])
        if fact.startswith("time:"):
            return fact.partition("|")[2] or fact[len("time:") :]
    except (KeyError, ValueError):  # a shape nobody anticipated: say it as it is
        return fact
    return fact


# --- What the user typed, as far as an answer may repeat it ---------------------
#
# A figure the user gives about THEIR OWN situation is a given: "I have 70
# percent attendance", "I'm at 70%", "with 70 percent attendance", "my family
# earns two lakh", "a student with 60 percent". An answer that works with it has
# to be able to say it back, and rejecting "at 70 percent you may apply for
# condonation" for containing 70 turned a correct answer into "not found".
#
# A figure the user asks ABOUT, or only reports hearing or checking, is a claim
# or a target, not a given: "Is the minimum 70 percent?", "What does the bus pass
# cost for 2023-24?", "Can I borrow 10 books?", "We heard the hostel closes on 15
# August", "I am checking whether the hostel fee is 50000". An answer that states
# one of those has to find it in the passages: echoing it back is how an answer
# agrees with a wrong number or answers for a year the documents do not cover.
#
# Two earlier designs judged whole CLAUSES by words anywhere in them, and lost both
# ways: a first-person verb anywhere let "I got a mail with the hostel fee 50000"
# through, and one doubt word anywhere refused "I have 70 percent attendance, what
# is the minimum?". So each FIGURE is judged on its own, by what stands right
# before it, and a figure that is not the user's own is masked out of what the
# answer may repeat:
#
# - an ownership cue close before it: "I have", "I'm", "I scored", "my X is", "a
#   student with", or "with"/"at"/"having" right before it, or a personal
#   attribute named with it ("attendance is 70", "70 percent attendance" opening a
#   sentence);
# - and nothing before it in its clause that only reports, checks or hears
#   ("heard", "says", "whether", "checking", "regarding", "screenshot"...);
# - and not right after a noun that names what an institution CHARGES or REQUIRES
#   ("fee", "fine", "minimum", "limit"...), unless the clause is in the past ("my
#   hostel fee WAS 55000" is what they paid; "the hostel fee is 50000" is a claim);
# - and in a clause that opens with a question word, only when "with"/"at" or an
#   ownership cue that is not the question's own subject ("Can I get...") says so.
#
# What no rule can do is tell "I paid 50000 as hostel fee last year" (their own
# payment, which an answer needs to be able to mention) from the same words as a
# claim about the fee: that stays a given, and a model that agrees with it as policy
# is then the model's mistake, not a figure the check can catch.
_USER_CLAUSES = re.compile(
    r"(?<=[.!?;])\s+|\n+|,\s+|\s+(?:and|but|so|because|while|although)\s+",
    re.IGNORECASE,
)
_PEOPLE = r"(?:student|candidate|employee|member|applicant|resident|person|someone)"
# The apostrophes a phone keyboard types.
_APOS = "['" + chr(0x2018) + chr(0x2019) + chr(0x2BC) + "]"
_I = (
    r"(?:i|i"
    + _APOS
    + r"m|i"
    + _APOS
    + r"ve|i"
    + _APOS
    + r"d|we|we"
    + _APOS
    + r"re|we"
    + _APOS
    + r"ve)"
)
_CONTRACTED_I = (
    r"\b(?:i" + _APOS + r"m|i" + _APOS + r"ve|we" + _APOS + r"re|we" + _APOS + r"ve)\b"
)
_OWN_STATE = re.compile(
    r"\b" + _I + r"\s+(?:\w+\s+){0,2}?"
    r"(?:have|had|has|am|was|were|are|scored?|secured?|got|get|earn(?:s|ed)?"
    r"|paid|pay|joined|enrolled|admitted|completed|passed|failed|attended|missed"
    r"|owe|owed|graduated|born|started|began|entered|registered|applied|appeared"
    r"|submitted|received|bought|purchased)\b"
    r"|" + _CONTRACTED_I + r"|\b(?:my|our|mine)\s+(?:\w+\s+){0,3}?"
    r"(?:is|was|are|were|has|have|had|earn(?:s|ed)?|stands?|comes?|came|totals?"
    r"|scored?|secured?)\b"
    r"|\b" + _PEOPLE + r"s?\s+(?:with|who\s+(?:has|have|had|scored?|secured?|got"
    r"|earn(?:s|ed)?))\b"
    r"|\bas\s+an?\s+(?:\S+\s+){0,2}?(?:student|scholar|candidate|employee|member"
    r"|applicant|resident)\b",
    re.IGNORECASE,
)
_THIRD_PERSON_WITH = re.compile(r"\b" + _PEOPLE + r"s?\s+with\b", re.IGNORECASE)
_DOUBT = re.compile(
    r"\b(?:heard|hear|read|told|tell|says?|said|think|thought|believe[ds]?"
    r"|rumou?r\w*|supposed\w*|apparently|claims?|true|right|correct|sure"
    r"|confirm\w*|enough|fine|okay|ok|allowed|permitted|eligible|need|needs"
    r"|needed|require[sd]?|must|minimum|maximum|limit|cut-?off|deadline|ask\w*)\b",
    re.IGNORECASE,
)


def user_stated_clauses(user_text) -> list:
    """The clauses of what the user typed that state THEIR OWN situation, judged
    by words anywhere in them. For what a QUESTION is about (quoting.py sets such
    clauses aside); what an answer may repeat is user_stated_text."""
    kept = []
    for clause in _USER_CLAUSES.split(user_text or ""):
        clause = clause.strip()
        if not clause or not _OWN_STATE.search(clause) or _DOUBT.search(clause):
            continue
        if "?" in clause and not _THIRD_PERSON_WITH.search(clause):
            continue
        kept.append(clause)
    return kept


# A figure in what the user typed: digits with their grouping, or a spelled number
# ("two lakh", "sixty-five").
_NUMBER_WORDS = "|".join(
    sorted({*_UNITS, *_TENS, *_SCALES, "hundred"}, key=len, reverse=True)
)
_USER_FIGURE = re.compile(
    r"\d(?:[\d,.:/]*\d)?|\b(?:"
    + _NUMBER_WORDS
    + r")(?:[ -]+(?:"
    + _NUMBER_WORDS
    + r"))*\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_APOSTROPHE_FOLD = {0x2018: 0x27, 0x2019: 0x27, 0x2BC: 0x27}
_QUESTION_WORDS = frozenset(
    "is are am do does did can could may will would should shall what which how "
    "when where why who whom whose".split()
)
# Before a figure: the user is reporting, hearing or checking it, not stating it.
_REPORTED = re.compile(
    r"\b(?:heard|hear|read|told|tell|says?|said|think|thought|believe[ds]?"
    r"|rumou?r\w*|supposed\w*|apparently|claims?|whether|wonder\w*|check\w*"
    r"|verif\w*|quer(?:y|ies)|regarding|screenshot|mail|email|looked|looking|saw"
    r"|seen|shows?|showing|notes?|know|aware|expect\w*|suspect\w*|assum\w*"
    r"|confirm\w*)\b",
    re.IGNORECASE,
)
# What an institution charges or requires: a figure right after one is a claim
# about it, unless the clause is in the past ("my hostel fee WAS 55000").
_POLICY_NOUNS = frozenset(
    "fee fees fine fines charge charges cost costs price prices tuition penalty "
    "limit minimum maximum cutoff deadline threshold requirement quota rate "
    "stipend".split()
)
_PAST_WORDS = frozenset("was were paid had charged spent".split())
# What a person has: "attendance is 70", "70 percent attendance".
_PERSONAL_NOUNS = frozenset(
    "attendance marks mark score scores cgpa gpa percentage rank income aggregate "
    "backlog backlogs age".split()
)
_STATE_WORDS = frozenset("is was at of stands".split())
# How close before a figure an ownership cue has to end, and how far from the
# user's own figure the next one may stand and still be part of it ("15 August
# 2025", "the 2021-25 batch", "4500 for my bus pass in 2023").
_CUE_REACH = 40
_SAME_FIGURE_GAP = 24


def _mask_clause(clause: str):
    """(the clause with every figure that is not the user's own replaced by "_",
    how many figures are)."""
    text = clause.translate(_APOSTROPHE_FOLD).replace("cut-off", "cutoff")
    lowered = text.lower()
    first_words = _WORD.findall(lowered)
    asking = bool(first_words) and first_words[0] in _QUESTION_WORDS
    pieces, last, own, last_own_end = [], 0, 0, None
    for match in _USER_FIGURE.finditer(text):
        before = lowered[: match.start()]
        words = _WORD.findall(before)
        after = _WORD.findall(lowered[match.end() :])[:3]

        # A cue owns the figure(s) of its own predicate: one with another figure
        # between it and this one does not reach it, unless that figure is the
        # user's own and close ("15 August 2025").
        owner = (
            last_own_end is not None
            and match.start() - last_own_end <= _SAME_FIGURE_GAP
        )
        for found in _OWN_STATE.finditer(before):
            if len(before) - found.end() > _CUE_REACH:
                continue
            if _USER_FIGURE.search(before[found.end() :]):
                continue
            # "Can I get ...", "Do I have ...": the pronoun is the question's own.
            if asking and len(_WORD.findall(before[: found.start()])) < 3:
                continue
            owner = True
        situational = bool(words) and words[-1] in ("with", "at", "having")
        personal = not asking and (
            (
                len(words) >= 2
                and words[-1] in _STATE_WORDS
                and words[-2] in _PERSONAL_NOUNS
            )
            or (not words and any(word in _PERSONAL_NOUNS for word in after[:2]))
        )
        reported = bool(_REPORTED.search(before))
        policy = any(word in _POLICY_NOUNS for word in words[-3:]) and not any(
            word in _PAST_WORDS for word in words
        )

        if (owner or situational or personal) and not (reported or policy):
            own += 1
            last_own_end = match.end()
            continue
        pieces.append(text[last : match.start()])
        pieces.append("_")
        last = match.end()
    pieces.append(text[last:])
    return "".join(pieces), own


def user_stated_text(user_text) -> str:
    """What the user typed, reduced to the figures they gave about themselves (see
    the comment above for what counts): each clause that holds one, with every
    other figure in it masked. Only figures in this text may be repeated by an
    answer."""
    kept = []
    for clause in _USER_CLAUSES.split(user_text or ""):
        clause = clause.strip()
        if not clause:
            continue
        masked, own = _mask_clause(clause)
        if own:
            kept.append(masked.strip())
    return " ".join(kept)
