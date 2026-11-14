"""The same fact, written two ways.

Grounding refuses an answer that states a figure no cited passage contains,
and that is the guarantee this system exists for. But a check on TOKENS
rejects a correct answer for how it spells a fact:

    passage "31.03.2025"            answer "31 March 2025"
    passage "the 15th day of March, 2025"   answer "15 March 2025"
    passage "Fee Structure 2024-25" answer "the 2024-2025 fee"
    passage "within seven days"     answer "within 7 days"
    answer  "[1, 2]"                (one citation, written as a group)

Each of those threw away a whole, correct, cited answer and showed the reader
"I couldn't find enough information". The helpers here recognise those
spellings and reduce them to one form on both sides, and nothing wider. Every
widening is also a way to accept a WRONG figure, so each one is narrow:

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
  have supported any answer that said 1.
- A range of years supports each of its years only when they are consecutive
  ("2024-25" is one academic year). "Rs 1000-2000" does not support "Rs 2000".
- Only ASCII is matched. Case-insensitive matching otherwise treats "\N{LATIN SMALL LETTER LONG S}"
  as an "s", and a month name that is not in the table must never be an error:
  an answer or a document that spells one oddly cannot be allowed to make the
  check itself fail.
"""

import datetime
import re

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

# "[1, 2]" and "[1; 2]": a model asked to cite as [1] often groups them.
GROUPED_MARKER = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})+)\]", re.ASCII)

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
# What a number can count in an institution's documents. A number word counts
# only when one of these follows it (with at most one qualifier between:
# "ten working days"), because "one" and "second" turn up in nearly every
# passage and mean nothing as a figure.
_COUNTED = (
    "day|days|week|weeks|month|months|year|years|hour|hours|minute|minutes"
    "|semester|semesters|term|terms|credit|credits|mark|marks|book|books|copy"
    "|copies|attempt|attempts|time|times|percent|rupee|rupees|lakh|crore"
    "|student|students|member|members|course|courses|subject|subjects|paper"
    "|papers|question|questions|page|pages|word|words|character|characters"
    "|installment|installments|instalment|instalments|session|sessions|class"
    "|classes|lecture|lectures|period|periods|batch|batches|seat|seats|room"
    "|rooms|copy|copies"
)
_NUMBER_WORD = re.compile(
    r"\b(?:(" + "|".join(_TENS) + r")(?:[- ](one|two|three|four|five|six|seven|eight"
    r"|nine))?|(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r"))"
    r"\s+(?:(?:calendar|working|academic|full|additional|consecutive)\s+)?"
    r"(?:" + _COUNTED + r")\b",
    _FLAGS,
)


def normalize_markers(text: str) -> str:
    """ "[1, 2]" as "[1][2]", which is how a citation is recognised."""
    return GROUPED_MARKER.sub(
        lambda match: "".join(
            f"[{n}]" for n in re.findall(r"\d{1,3}", match.group(1), re.ASCII)
        ),
        text,
    )


def prepare(text: str) -> str:
    """Text with year ranges written one way, before figures are read from it."""
    return _YEAR_RANGE_DASH.sub(r"\1-\2", text)


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


_SHORT_RANGE_IN_TEXT = re.compile(r"\b(\d{4})-(\d{2})\b", re.ASCII)


def prose_form(text: str) -> str:
    """Text with dates and year ranges in one form, for the word-overlap check.

    That check compares words, so "31st March 2025" against "31.03.2025", or
    "2024-25" against "2024-2025", counted as unsupported words even when the
    figure check had accepted them.
    """
    text, _ = extract_dates(prepare(text), inline=True)
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


def number_words(text: str) -> set:
    """The numerals a passage's number words stand for, when they count something.

    "seven days" and "twenty-five rupees" as "7" and "25". For the passage side
    only, and never for "one" or an ordinal on its own (see the module
    docstring): "No one may attend", "the second semester".
    """
    numerals = set()
    for match in _NUMBER_WORD.finditer(text):
        tens, unit, small = match.groups()
        if tens:
            value = _TENS.get(tens.lower())
            if value is None:
                continue
            if unit:
                value += _UNITS.get(unit.lower(), 0)
        else:
            value = _UNITS.get(small.lower())
            if value is None:
                continue
        numerals.add(str(value))
    return numerals
