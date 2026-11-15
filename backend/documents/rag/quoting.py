"""Helpers for the quoting answerer: what a question asks for, what kind of
line a sentence is, and what a question names that the passages never mention.

The quoting answerer (llm.ExtractiveAnswerer) is what a reader gets whenever
no AI model can write the answer - at capacity, out of its free quota for the
day, down. It selects sentences by the words they share with the question. That
is all it can do, and measured against real documents with every model call
failing it made mistakes a word-overlap ranker makes, each of which looked to
the reader like an answer:

- It quoted a document TITLE or a clause HEADING ("FEE STRUCTURE 2024-25") on
  the strength of two shared words, because a heading is short and the
  tie-break prefers short.
- It answered one half of a two-part question: the coverage floor was measured
  over the terms of BOTH parts, so neither part's sentence cleared it alone.
- It skipped a table row whose question named a COLUMN ("tuition") that only
  the header row spells.
- It treated "B.Tech" and "M.Tech" as the same word (a one-letter token is
  dropped), so the wrong programme's row could come first.
- It dumped a whole fee table for a programme (PhD, B.Arch) the table does not
  have, and quoted the 2024-25 bus pass price for "2023-24".
- It quoted a sentence cut mid-word at a chunk boundary ("f member is entitled
  to 12 days...") and one cut off at its end.

Nothing here reads the passages semantically. Each helper is a narrow,
checkable rule, and the ones that REFUSE (names_what_the_passages_lack) refuse
only on something a reader can see for themselves: a code the passages' own
table is keyed by, or a year, that the passages do not have. A refusal is the
"not found" a reader sees, so each rule is as narrow as it can be made: a
question that merely mentions a year, a room number or a capitalised word is
still answered.
"""

import re

from . import facts as fact_forms
from .reranking import STOPWORDS, _terms, stem

# A line that ends one of these is a sentence; one that does not and is short
# is a title, a clause heading or a list label.
SENTENCE_END = (".", "!", "?", ";")
MAX_HEADING_WORDS = 12

# A sentence this long that ends without punctuation was cut off by the end of
# its chunk, not written that way.
MIN_CUT_END_WORDS = 13

# What the question asks for. A figure or a date is the answer to "how much",
# "how long", "when", "by which date"; a sentence that has none cannot be it.
FIGURE_QUESTION = re.compile(
    r"\bhow\s+(?:much|many|long|often|far|old|soon|late)\b"
    r"|\b(?:fee|fees|fine|fines|cost|costs|price|charge|charges|penalty|limit"
    r"|minimum|maximum|percentage|percent|amount|rate|duration|period|number"
    r"|salary|stipend)\b",
    re.I,
)
DATE_QUESTION = re.compile(
    r"\bwhen\b|\bby\s+when\b|\bby\s+which\s+date\b|\bdeadline\b"
    r"|\bwhat\s+(?:\w+\s+){0,2}?(?:date|day|time|year|month)\b"
    r"|\bwhich\s+(?:\w+\s+){0,2}?(?:date|day|year|month)\b"
    r"|\blast\s+(?:date|day)\b",
    re.I,
)
SPELLED_NUMBER = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty"
    r"|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|crore)\b",
    re.I,
)

# A code like B.Tech, M.Tech, B.Arch, Ph.D: letters, a dot, letters. Matching
# drops one-letter words, so "B.Tech" and "M.Tech" were both just "tech".
DOTTED_CODE = re.compile(r"\b[A-Za-z]{1,6}(?:\.[A-Za-z]{1,10})+\b")
# Words that look like codes and are not.
NOT_A_CODE = frozenset({"eg", "ie", "vs", "etc", "dr", "mr", "mrs", "ms", "no", "st"})

# Identifiers a question can name: a dotted code, a mixed-case code (PhD) or an
# acronym of three letters or more (CSE, MBA). Two letters ("IT") is too short
# to tell from a paraphrase ("IT lab" for "computer lab").
MIXED_CASE = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b")
ACRONYM = re.compile(r"\b[A-Z]{3,}\b")

# What a capitalised word in a question is NOT evidence of. A question can
# shout ("PLEASE", "ASAP", "SUNDAYS"), name a product ("WiFi") or use an
# abbreviation the documents spell out ("HOD" for "head of department", "INR"
# for "rupees"): none of these is a row the passages should have. These are
# ordinary English and common abbreviations, not words taken from any one
# corpus; the question's own words, and the words the retrieved passages use in
# lower case, are checked as well (_is_ordinary_word).
_ORDINARY_CAPS = frozenset(
    """asap urgent urgently only really very just all any now today immediately
    always never must important kindly sorry thanks thank again also exactly
    quickly clearly hurry help needed required mandatory compulsory free fast
    sure maybe yes not none nothing everything everyone anyone someone
    monday tuesday wednesday thursday friday saturday sunday weekday weekdays
    weekend weekends mondays tuesdays wednesdays thursdays fridays saturdays
    sundays january february march april june july august september october
    november december
    pdf doc docx xls xlsx ppt pptx jpg jpeg png gif zip csv txt url faq otp pin
    atm upi sms gps cctv fyi tbd rsvp eta
    wifi youtube linkedin whatsapp powerpoint facebook instagram github google
    inr usd eur gst vat
    hod cgpa sgpa gpa ncc nss ews obc nptel swayam mooc ugc aicte""".split()
)

# One- and two-letter words a chunk can start with. Anything else short at the
# start of a lowercase fragment is the end of a word the chunk boundary cut.
_SHORT_WORDS = frozenset(
    "a an as at be by do go he if in is it me my no of oh on or so to up us we".split()
)


def _is_real_start(token: str) -> bool:
    """Whether a lowercase-led token a text starts with is a word or a label
    rather than the end of a word a chunk boundary cut: one with a capital
    after its first letter (pH, eBooks, iPhone, tV), or a list label or
    hyphenated start ("a)", "i.", "e-")."""
    return any(char.isupper() for char in token[1:]) or token.endswith((".", ")", "-"))


# The words of a question for a date that name the KIND of answer rather than
# its subject, as the stemmed terms the answerer compares.
FORM_TERMS = frozenset(stem(word) for word in ("last", "day", "date", "deadline"))


def wants_figure(question: str) -> bool:
    """Whether the question asks for a figure or a date."""
    return bool(FIGURE_QUESTION.search(question) or DATE_QUESTION.search(question))


def wants_date(question: str) -> bool:
    """Whether the question asks for a date or a year."""
    return bool(DATE_QUESTION.search(question))


# A month in words, for "in July". "May" and "March" are left out: they are
# also a verb and a modal, and a sentence about what a student may do is not a
# sentence about a date.
MONTH_WORD = re.compile(
    r"\b(?:january|february|april|june|july|august|september|october|november"
    r"|december|jan|feb|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
    re.I,
)


def has_figure(sentence: str) -> bool:
    return bool(
        re.search(r"\d", sentence)
        or SPELLED_NUMBER.search(sentence)
        or MONTH_WORD.search(sentence)
    )


# The "Clause 12.5", "Section 3" or "2." a heading starts with is a label, not
# a figure the heading states.
_LABEL = re.compile(
    r"^\s*(?:clause|section|article|rule|paragraph|para|chapter|annexure|appendix"
    r"|item|step)?\s*[A-Za-z]?\d+(?:\.\d+)*[.)]?\s*",
    re.I,
)


def heading_has_figure(sentence: str) -> bool:
    """Whether a heading states a figure or a date, not counting its own
    numbering."""
    return has_figure(_LABEL.sub("", sentence, count=1))


def code_terms(text: str) -> set:
    """Dotted codes as single terms: "B.Tech" is "btech", "M.Tech" is "mtech"."""
    codes = set()
    for match in DOTTED_CODE.finditer(text):
        letters = re.sub(r"[^a-z]", "", match.group(0).lower())
        if len(letters) >= 3 and letters not in NOT_A_CODE:
            codes.add(letters)
    return codes


def quote_terms(text: str) -> set:
    """The terms two texts are compared by: the shared content terms, and the
    dotted codes the shared definition cannot see."""
    return set(_terms(text)) | code_terms(text)


# What may follow a sentence's last full stop without changing how it ends: the
# quote or bracket that closes it.
_CLOSERS = ")]}\"'\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"


def _terminated(text: str) -> bool:
    """Whether the text ends the way a sentence does. A colon counts: "The
    following apply:" is an introduction, not a title."""
    core = text.rstrip().rstrip(_CLOSERS).rstrip()
    return core.endswith(SENTENCE_END + (":",))


# What a clause or section heading starts with ("Clause 4.2: Attendance").
_CLAUSE_LABEL = re.compile(
    r"^\s*(?:clause|section|article|rule|paragraph|para|chapter|annexure|appendix"
    r"|item|step|part|unit|schedule)\b",
    re.I,
)
# "Casual leave: 12 days per calendar year": a short label, a colon, a value.
_LABEL_VALUE = re.compile(r"^([^:]{1,60}):\s*\S")
MAX_LABEL_WORDS = 6
# The label of a memo or circular header ("Subject: Campus bus services for the
# academic year 2024-25") introduces a TITLE, not a fact.
_TITLE_LABELS = frozenset(
    "subject title re ref reference topic heading regarding sub".split()
)
# A list item: a bullet, "a)", "(iv)", "(2)". A bare "1." is judged below.
_LIST_ITEM = re.compile(
    r"^\s*(?:[-*•–—]\s+|\(?[a-z][.)]\s+|\(?[A-Za-z]\)\s+"
    r"|\(?(?:ii|iii|iv|vi|vii|viii|ix)[.)]\s+|\(\d{1,2}\)\s+)\S"
)
_NUMBERED_ITEM = re.compile(r"^\s*\d{1,2}[.)]\s+(\S.*)$")


_TITLE_STOPWORDS = frozenset("for of and the to in on at a an".split())
_YEAR_OR_RANGE = re.compile(r"(?:19|20)\d{2}(?:[-/]\d{2,4})?")


def _looks_like_a_title(value: str) -> bool:
    """Whether what follows a label is a TITLE ("Staff Leave Policy 2024-25",
    "Bus Pass Renewal"): three or more words, every one capitalised, a short
    little word or a year. "Casual leave: 12 days", "Fee: Rs 500" and "Warden:
    Dr Rao" are facts; "Policy: Staff Leave Policy 2024-25" is a heading whose
    label ("Policy", "Notice", "Circular", "Guidelines") nobody can list."""
    words = [word.strip(".,;:()") for word in value.split()]
    words = [word for word in words if word]
    if len(words) < 3:
        return False
    for word in words:
        if word.lower() in _TITLE_STOPWORDS or _YEAR_OR_RANGE.fullmatch(word):
            continue
        if not word[:1].isupper():
            return False
    return True


def _is_data_line(text: str) -> bool:
    """A "label: value" line or a list item. Documents hold facts this way
    ("Casual leave: 12 days", "Monday to Saturday: 9 am to 8 pm", "- Admission
    letter"), and a line like that is the fact, not a title above one."""
    if _LIST_ITEM.match(text):
        return True

    item = _NUMBERED_ITEM.match(text)
    if item:
        # "2. Fee receipt" is an item of a list; "3. Library Rules" and
        # "4. ATTENDANCE" are numbered headings, which every word capitalised
        # (or in capitals) gives away. A single word is left a heading.
        words = item.group(1).split()
        return len(words) > 1 and any(word[:1].islower() for word in words[1:])

    labelled = _LABEL_VALUE.match(text)
    if labelled:
        label = labelled.group(1)
        return (
            not _CLAUSE_LABEL.match(label)
            and label.strip().lower() not in _TITLE_LABELS
            and len(label.split()) <= MAX_LABEL_WORDS
            and not _looks_like_a_title(text[labelled.end(1) + 1 :])
        )
    return False


def is_heading(sentence: str, *, is_row: bool) -> bool:
    """A title, a clause heading or a list label rather than a sentence.

    No full stop and a dozen words or fewer. A table row is never one, nor is a
    "label: value" line or a list item (a fact set out as a line), and a heading
    is not always useless ("Subject: Campus bus services for the academic year
    2024-25" IS the answer to "which year"), so the answerer quotes one only
    when nothing else qualifies. "FEE STRUCTURE 2024-25", "Library Hours" and
    "Clause 3.1 Visitors" are headings.
    """
    text = sentence.strip()
    if is_row or not text or _terminated(text) or _is_data_line(text):
        return False
    return len(text.split()) <= MAX_HEADING_WORDS


def begins_mid_text(text: str) -> bool:
    """Whether a passage starts in the middle of a word or a sentence.

    A passage that starts with a lowercase letter was cut by a chunk boundary,
    except one that starts with a word that is written that way (pH, eBooks,
    iPhone) or a list label ("a) ..."): marking those would tell the reader the
    text continues from before when it does not.
    """
    text = text.lstrip()
    if not text or not text[0].islower():
        return False
    return not _is_real_start(text.split(None, 1)[0])


def trim_cut_start(sentence: str):
    """(sentence, whether it began mid-word or mid-sentence).

    A chunk boundary can fall inside a word: "f member is entitled..." is the
    end of "staff". A leftover of one or two letters that is not a word is
    dropped; the caller marks the start with an ellipsis, so the reader is told
    the sentence continues from before and the quote is still a substring of
    the passage. A token that is a real word or label (pH, eBooks, "a)", "e-")
    is never dropped, and a sentence that starts with one is not marked.
    """
    text = sentence.lstrip()
    if not text or not text[0].islower():
        return sentence, False
    first, _, rest = text.partition(" ")
    if _is_real_start(first):
        return text, False
    if len(first) <= 2 and first not in _SHORT_WORDS and rest.strip():
        return rest.lstrip(), True
    return text, True


def ends_cut(sentence: str) -> bool:
    """A long sentence that stops without punctuation: its chunk ended there."""
    text = sentence.rstrip()
    return (
        bool(text)
        and not _terminated(text)
        and (len(text.split()) >= MIN_CUT_END_WORDS)
    )


def _identifier_tokens(text: str, *, allow_shouting: bool = False) -> dict:
    """The codes a text names, as {letters: kind}: "dotted" (B.Tech, Ph.D),
    "mixed" (PhD, CoE) or "caps" (CSE, MBA). A dotted code is the strongest
    evidence, an acronym the weakest."""
    found = {}
    # A question typed in capitals is shouting, not naming: every word of it
    # would be an "acronym". A dotted code is still a code.
    shouting = text.isupper() and not allow_shouting
    for kind, pattern in (
        ("dotted", DOTTED_CODE),
        ("mixed", MIXED_CASE),
        ("caps", ACRONYM),
    ):
        if shouting and kind != "dotted":
            continue
        for match in pattern.finditer(text):
            word = match.group(0)
            letters = re.sub(r"[^a-z]", "", word.lower())
            if len(letters) < 3 or letters in NOT_A_CODE:
                continue
            # iPhone, eBooks: a product word that starts in lower case is not
            # a programme or a degree.
            if kind == "mixed" and word[0].islower():
                continue
            found.setdefault(letters, kind)
    return found


_LOWER_WORD = re.compile(r"\b[a-z]{3,}\b")


def _is_ordinary_word(token: str, support: str) -> bool:
    """Whether a capitalised token is an ordinary word or a common
    abbreviation: on the list above, or a word the passages themselves write in
    lower case ("only", "sundays")."""
    root = stem(token)
    if token in _ORDINARY_CAPS or root in _ORDINARY_CAPS or token in STOPWORDS:
        return True
    return any(stem(word) == root for word in _LOWER_WORD.findall(support))


def _letters_only(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


# A year range ("2024-25", "2023-2024") and a bare year (19xx, 20xx) in text
# that has had its full dates taken out.
_YEAR_RANGE = re.compile(
    r"(?<![\d.,/-])((?:19|20)\d{2})-((?:19|20)\d{2}|\d{2})(?![\d-])"
)
_BARE_YEAR = re.compile(r"(?<![\w.,/-])((?:19|20)\d{2})(?![\w-]|[.,]\d)")
# 2000 is also an amount: "Rs 2000", "2000 rupees", "1990 pages" are not years.
_CURRENCY_BEFORE = re.compile(r"(?:\brs\.?|\binr|\busd|\beur|[₹$€£])\s*$", re.I)
_QUANTITY_AFTER = re.compile(
    r"\s*(?:%|per\b|percent\b|rs\b|rupees?\b|inr\b|usd\b|dollars?\b|pages?\b"
    r"|credits?\b|marks?\b|books?\b|words?\b|characters?\b|[kmg]b\b|copies\b)",
    re.I,
)
# A range of years states each year in it, up to this many.
MAX_RANGE_SPAN = 10


def _range_stated(asked: str, known: set) -> bool:
    """Whether a year range is one a passage states, or lies inside a longer
    one it states ("2019-2025" covers "2023-2024")."""
    first, last = (int(year) for year in asked.split("-"))
    for other in known:
        start, end = (int(year) for year in other.split("-"))
        if start <= first and last <= end:
            return True
    return False


def year_facts(text: str) -> tuple:
    """(year ranges, years) that `text` states, as strings.

    "2024-25" is the range "2024-2025" and also the years 2024 and 2025;
    "15 August 2025" states the year 2025 and "15 August" none. Only a year or
    a year range counts: a room number, a roll number or an amount is not
    something a document is "for", so a question that names one is not
    refused for it (see names_what_the_passages_lack).
    """
    remainder, dates = fact_forms.extract_dates(fact_forms.prepare(text))
    years = set()
    ranges = set()
    for date in dates:
        match = re.fullmatch(r"date:(\d{4})-\d{2}-\d{2}", date)
        if match:
            years.add(match.group(1))

    def take_range(match):
        first = int(match.group(1))
        tail = match.group(2)
        last = int(tail) if len(tail) == 4 else first // 100 * 100 + int(tail)
        if len(tail) == 2 and last < first:
            last += 100
        if last < first:
            return match.group(0)
        ranges.add(f"{first}-{last}")
        if last - first <= MAX_RANGE_SPAN:
            years.update(str(year) for year in range(first, last + 1))
        else:
            years.update((str(first), str(last)))
        return " "

    remainder = _YEAR_RANGE.sub(take_range, remainder)
    for match in _BARE_YEAR.finditer(remainder):
        if _CURRENCY_BEFORE.search(remainder[: match.start()]):
            continue
        if _QUANTITY_AFTER.match(remainder[match.end() :]):
            continue
        years.add(match.group(1))
    return ranges, years


def _without_own_situation(question: str) -> str:
    """The question with the clauses in which the user describes THEMSELVES taken
    out ("I am a B.Tech student", "I joined in 2022", "as a PhD scholar"): who is
    asking is not what is asked. A question that names its asker's programme or
    year is about the rule, not about that programme or year."""
    for clause in fact_forms.user_stated_clauses(question):
        question = question.replace(clause, " ")
    return question


def own_situation_terms(question: str, passages) -> frozenset:
    """The terms that ONLY the user's description of themselves contributes and
    that no retrieved passage contains: "am", "batch", "2021" and "25" in "I am in
    the 2021-25 batch. How much is the bus pass?".

    A sentence is measured by the share of the question's terms it covers, and no
    sentence of a circular covers the asker's batch: left in, those terms sank the
    one that answers all of what was asked ("A bus pass costs 4500 rupees per
    semester" covered 2 of 6, under the floor). A term some passage DOES contain
    stays, because it can pick a row: "I am a B.Tech CSE student, what is my
    tuition?" is answered from the CSE row."""
    clauses = fact_forms.user_stated_clauses(question)
    if not clauses:
        return frozenset()
    mine = set()
    for clause in clauses:
        mine |= quote_terms(clause)
    mine -= quote_terms(_without_own_situation(question))
    if not mine:
        return frozenset()
    present = set()
    for passage in passages:
        present |= quote_terms(passage.text)
        present |= quote_terms(getattr(passage, "title_text", "") or "")
        present |= quote_terms(" ".join(getattr(passage, "table_header", ()) or ()))
    return frozenset(mine - present)


# A word that can stand just before a code that is the thing asked about.
_KEY_BEFORE = frozenset("for of in to about on at".split())
_ARTICLES = frozenset("the a an our your their".split())
_QUESTION_WORD = re.compile(r"[\w.]+")


def _positioned_like_a_key(question: str, token: str) -> bool:
    """Whether an ACRONYM sits where a table's row key would: right after "for",
    "of", "in" ("the tuition for MBA"), or next to a dotted code ("B.Tech CSE").
    An emphasis word typed in capitals ("CHECK what is the fee", "per YEAR")
    sits nowhere in particular."""
    words = _QUESTION_WORD.findall(question)
    for index, word in enumerate(words):
        if re.sub(r"[^a-z]", "", word.lower()) != token:
            continue
        back = index - 1
        while back >= 0 and words[back].lower() in _ARTICLES:
            back -= 1
        before = words[back] if back >= 0 else ""
        after = words[index + 1] if index + 1 < len(words) else ""
        if before.lower() in _KEY_BEFORE:
            return True
        if DOTTED_CODE.fullmatch(before) or DOTTED_CODE.fullmatch(after):
            return True
    return False


def _code_gap_is_visible(question: str, passages) -> bool:
    """Whether the retrieved passages ENUMERATE codes, so that one they lack is a
    gap and not just a different way of saying something.

    Prose that names codes ("B.Tech tuition is 85000 and M.Tech 95000") does. A
    table keyed by codes does, but only for a question that asks for what its
    columns hold ("tuition", "hostel"): asked the library fine, a fee table that
    happens to be retrieved says nothing about your programme. Passages that name
    no code at all (an attendance rule, a curfew) leave nothing to be missing: the
    rule applies to a B.Tech student, an EEE student and a PhD scholar alike.
    """
    asked = quote_terms(question)
    for passage in passages:
        if not _identifier_tokens(passage.text, allow_shouting=True):
            continue
        if not getattr(passage, "is_table", False):
            return True
        header = tuple(getattr(passage, "table_header", ()) or ())
        values = quote_terms(" ".join(header[1:] if len(header) > 1 else header))
        if not header or asked & values:
            return True
    return False


def names_what_the_passages_lack(question: str, passages) -> str:
    """Why the passages cannot answer THIS question, or "" if they might.

    A word-overlap ranker has no way to notice that the question is about
    something else. Two things it can see for itself, and that a reader sees
    at once, are a CODE (PhD, B.Arch, CSE) or a YEAR in the question that the
    passages cannot have: asked the tuition for a programme the fee table does
    not list, it quoted the table; asked the bus pass for 2023-24, it quoted
    the 2024-25 circular. Each rule is as narrow as it can be, because a
    refusal is the "not found" a reader sees:

    - What the user says about THEMSELVES is not what is asked ("I am a B.Tech
      student", "I joined in 2022"): those clauses are set aside first.
    - A code (B.Arch, PhD, MBA) that no passage contains is a gap only where the
      passages enumerate codes (prose naming programmes, or a table keyed by them
      and asked about), see _code_gap_is_visible. An acronym must also sit where
      a row key would, and not be an ordinary word or a common abbreviation:
      "PLEASE", "ASAP", "SUNDAYS", "PDF", "HOD" name nothing the passages should
      have.
    - A year (2025, 2023-24, "15 August 2025") must be one the passages state,
      but only when they state some year at all. A rule with no year in it
      (leave, curfew, attendance) is year-less: "in 2025" does not make it
      unanswerable. Other numbers (a room, a roll number, a premise like "250
      pages") are never grounds.

    Nothing is said about a word that is merely worded differently ("IT" for
    "computer", "complaint" for "grievance"): those stay with the overlap
    rules.
    """
    # Titles count as present here: this only decides whether to REFUSE, so
    # being generous about what a document mentions is the cautious direction.
    support = " ".join(
        f"{getattr(p, 'support_text', p.text)} {getattr(p, 'title_text', '')}"
        for p in passages
    )

    question = _without_own_situation(question)
    present = _letters_only(support)
    visible = None
    for token, kind in sorted(_identifier_tokens(question).items()):
        if token in present:
            continue
        if kind != "dotted" and _is_ordinary_word(token, support):
            continue
        if kind == "caps" and not _positioned_like_a_key(question, token):
            continue
        if visible is None:
            visible = _code_gap_is_visible(question, passages)
        if not visible:
            continue
        return (
            f"the question names {token.upper()!r}, which no retrieved "
            f"passage, title or column mentions"
        )

    asked_ranges, asked_years = year_facts(question)
    if asked_ranges or asked_years:
        known_ranges, known_years = year_facts(support)
        # No year in any passage or title: the rule is year-less.
        if known_ranges or known_years:
            missing_ranges = sorted(
                r for r in asked_ranges if not _range_stated(r, known_ranges)
            )
            # The years of an asked range are judged by the range, not twice.
            in_a_range = set()
            for asked_range in asked_ranges:
                first, last = (int(y) for y in asked_range.split("-"))
                in_a_range.update(str(y) for y in range(first, last + 1))
            missing = missing_ranges + sorted(
                y for y in asked_years - known_years if y not in in_a_range
            )
            if missing:
                return (
                    f"the question names {', '.join(missing)}, which no "
                    f"retrieved passage mentions"
                )

    return ""
