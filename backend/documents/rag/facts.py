"""The same fact, written two ways.

Grounding refuses an answer that states a figure no cited passage contains,
and that is the guarantee this system exists for. But a check on TOKENS
rejects a correct answer for how it spells a fact:

    passage "31.03.2025"            answer "31 March 2025"
    passage "Fee Structure 2024-25" answer "the 2024-2025 fee"
    passage "within seven days"     answer "within 7 days"
    answer  "[1, 2]"                (one citation, written as a group)

Each of those threw away a whole, correct, cited answer and showed the reader
"I couldn't find enough information". The helpers here recognise those
spellings and reduce them to one form on both sides, and nothing wider:

- Only dates that can be read one way. A month name or an ISO date always can.
  A numeric date can when one of its numbers is over 12 (31.03.2025) or both
  are equal. "04.03.2025" is the 4th of March or the 3rd of April, so it is
  left alone and must match as written - accepting either reading would accept
  a wrong date.
- A date is compared as a whole. Its day or its year never stands in for it,
  so "31 May 2025" is not supported by a passage that says "31 March 2025".
- Number words count on the PASSAGE side only: a passage that says "seven"
  supports an answer that says 7, but an answer's "one of the conditions" is
  not a figure the reader will act on, and is not turned into one.
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
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH + r")\b\.?,?\s+(\d{4})\b",
    re.I,
)
_MONTH_DAY_YEAR = re.compile(
    r"\b(" + _MONTH + r")\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")

# "2024-25" and "2024-2025" are one academic year, and a range typed with an
# en dash ("2024\N{EN DASH}25") is the same range as one typed with a hyphen.
_YEAR_RANGE_DASH = re.compile(r"\b(\d{4})\s*[\N{EN DASH}\N{EM DASH}]\s*(\d{2}|\d{4})\b")
_SHORT_RANGE = re.compile(r"(\d{4})-(\d{2})")
_LONG_RANGE = re.compile(r"(\d{4})-(\d{4})")

# "[1, 2]" and "[1; 2]": a model asked to cite as [1] often groups them.
_GROUPED_MARKER = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})+)\]")

_UNITS = {
    "zero": 0,
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
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_SMALL = "|".join(sorted((*_UNITS, *_ORDINALS), key=len, reverse=True))
_NUMBER_WORD = re.compile(
    r"\b(?:(" + "|".join(_TENS) + r")(?:[- ](one|two|three|four|five|six|seven|eight"
    r"|nine))?|(" + _SMALL + r"))\b",
    re.I,
)


def normalize_markers(text: str) -> str:
    """ "[1, 2]" as "[1][2]", which is how a citation is recognised."""
    return _GROUPED_MARKER.sub(
        lambda match: "".join(f"[{n}]" for n in re.findall(r"\d{1,3}", match.group(1))),
        text,
    )


def prepare(text: str) -> str:
    """Text with year ranges written one way, before figures are read from it."""
    return _YEAR_RANGE_DASH.sub(r"\1-\2", text)


def _canonical(year: int, month: int, day: int):
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None
    return f"date:{year:04d}-{month:02d}-{day:02d}"


def extract_dates(text: str, *, inline: bool = False) -> tuple[str, set]:
    """(text with each unambiguous date taken out, the dates as canonical tokens).

    A date that cannot be read one way, or is not a real date, stays in the
    text exactly as written. With `inline`, each date is replaced by one word
    that names it ("date20250331") instead of being taken out, which is what
    a word-overlap comparison needs: "31st March 2025" and "31.03.2025" share
    no words, and are one date.
    """
    found = set()

    def take(token, original):
        if token is None:
            return original
        found.add(token)
        if inline:
            return " " + token.replace(":", "").replace("-", "") + " "
        return " "

    def day_month_year(match):
        month = MONTH_NUMBER[match.group(2).lower()]
        return take(
            _canonical(int(match.group(3)), month, int(match.group(1))), match.group(0)
        )

    def month_day_year(match):
        month = MONTH_NUMBER[match.group(1).lower()]
        return take(
            _canonical(int(match.group(3)), month, int(match.group(2))), match.group(0)
        )

    def iso(match):
        return take(
            _canonical(int(match.group(1)), int(match.group(2)), int(match.group(3))),
            match.group(0),
        )

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
        return take(_canonical(year, month, day), match.group(0))

    for pattern, replace in (
        (_ISO_DATE, iso),
        (_DAY_MONTH_YEAR, day_month_year),
        (_MONTH_DAY_YEAR, month_day_year),
        (_NUMERIC_DATE, numeric),
    ):
        text = pattern.sub(replace, text)
    return text, found


_SHORT_RANGE_IN_TEXT = re.compile(r"\b(\d{4})-(\d{2})\b")


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
    """The two years of a "2024-2025" range."""
    match = _LONG_RANGE.fullmatch(fact)
    return set(match.groups()) if match else set()


def number_words(text: str) -> set:
    """The numerals a passage's number words and ordinals stand for.

    "seven" and "twenty-five" and "third" as "7", "25" and "3". For the
    passage side only (see the module docstring).
    """
    numerals = set()
    for match in _NUMBER_WORD.finditer(text):
        tens, unit, small = match.groups()
        if tens:
            value = _TENS[tens.lower()] + (_UNITS[unit.lower()] if unit else 0)
        else:
            word = small.lower()
            value = _UNITS[word] if word in _UNITS else _ORDINALS[word]
        numerals.add(str(value))
    return numerals
