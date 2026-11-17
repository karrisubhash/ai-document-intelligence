"""Grounding and citation validation.

An answer is not trusted because a model produced it. Before anything is
returned, four questions get asked of it:

1. Does every marker it cites exist in the context it was given? A citation
   to [7] when six passages were supplied is a fabricated reference, and it
   is the single cheapest hallucination to detect.
2. Did it cite anything at all? A substantive claim with no citation cannot
   be checked by the person reading it.
3. Does every FACT it states appear in the passages it cited? Numbers,
   percentages, money, dates and identifiers are the claims a reader acts
   on, and a wrong one is the expensive failure this system exists to
   prevent. This is a hard gate.
4. Is the prose as a whole drawn from those passages? Approximate by
   construction, so it is a loose backstop rather than the primary test.

A failure downgrades the answer to an abstention. Section 12.4 is explicit
that abstaining beats inventing.

WHY THIS WAS REWRITTEN, because the previous design failed in a way worth
recording. It had only checks 1, 2 and 4, and check 4 was the gate: at
least 55% of an answer's content words had to appear in the cited passages.
Measured against real answers over real passages, that produced:

    terse extractive quote          support 1.000   PASS
    close paraphrase, 2 sentences   support 0.647   PASS
    structured answer with headings support 0.456   REJECTED
    conversational answer           support 0.298   REJECTED

Both rejected answers were factually correct and fully cited. What sank
them was "answer", "detail", "deadline", "essentially", "before" - English
connective tissue. A bag-of-words overlap ratio cannot tell a fabricated
claim from a well-written sentence, because it is not measuring truth; it
is measuring how closely the prose echoes the source's vocabulary. As a
gate it therefore selects for answers that read like search results, and
rejects the ones that read like an explanation - while a fluent answer that
quietly changed 75 to 80 sails through, because "80" is one token among
sixty and the ratio barely moves.

So the gate moved to where invention actually shows up. Check 3 is blind to
style - "Great question! You'll need 75% [1]" and "The minimum is 75
percent [1]" carry the same facts - and unforgiving about substance: a
figure that is in no cited passage fails, however fluently it is written.
Check 4 survives at a much lower threshold, catching only wholesale
invention (an answer that cites a passage and then discusses something
else), and reports its ratio either way so the signal is still measurable.

What check 3 does NOT do, so nobody mistakes it for more: it compares figures,
not what they count. A wrong unit or noun beside a right number ("replaced
after 200 days" for "200 rupees") passes whenever the number is in a cited
passage, and so does a figure from one cited passage credited to the other.
The leniencies in rag/facts.py are all equivalences of one fact written two
ways; a figure the USER typed (`user_text`) is the one exception that is not
a passage, and it can only ever be echoed, never cited.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings

from documents.processing.tables import text_rows

from . import facts as fact_forms
from . import tables
from .llm import is_abstention
from .reranking import _terms

MARKER = re.compile(r"\[(\d{1,3})\]")

# Share of an answer's content words that must appear in the cited
# passages. This is the BACKSTOP, not the gate - see the module docstring.
# Set low enough that any legitimately-written answer passes and only
# wholesale invention fails: an answer that cites [1] and then discusses a
# subject [1] does not mention. Raising it back toward 0.55 re-creates the
# style filter this replaced.
MIN_SUPPORT_RATIO = 0.25

# Tokens that carry a fact a reader will act on. Deliberately narrow: the
# aim is the claims where being wrong is expensive and being approximate is
# not acceptable, not every noun in the answer.
#
#   75%  85,000  1,00,000  3.5  2024  REG/2024/117  section 4.2  $1,200
#
# Anything containing a digit qualifies. Words do not: "seven days" is
# covered by the prose backstop, and adding number-words here would mean
# maintaining a spelled-out-numeral table to catch a case that check 4
# already handles.
#
# The letters before the first digit are matched only from the START of a
# run of letters. Without that, a run of n letters was tried from each of its n
# places and the scan was quadratic: 20,000 letters took 18 seconds.
FACT = re.compile(r"(?<![A-Za-z])[A-Za-z]*\d[\w./,\-:]*")

# Markdown ordinals - "1." or "2)" opening a line - are formatting, not
# claims. Without this, every numbered list in an answer contributes facts
# that are not in any document and the answer is rejected for being tidy.
#
# However a model writes its list: "1.", "1)", "1:", "(1)", "**1.**" (the
# emphasis is stripped first), "### 1.", "> 1.", "* 1.", "1\.". The number may
# have one or two digits and MUST be followed by a mark and a space: "75
# percent" opening a line is a figure. Only blanks and tabs may stand before
# it, so a run of newlines is not a run of places to start looking.
ORDINAL_LINE = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:#{1,6}[ \t]+)?(?:[-*+\N{BULLET}][ \t]+)?"
    r"(?:\(\d{1,2}\)|\d{1,2}\\?[.):])(?:[ \t]+|$)",
    re.M,
)
_EMPHASIS = re.compile(r"\*\*|__")
_BLANK_LINES = re.compile(r"\n{3,}")


def _tidy(text: str) -> str:
    """An answer as its figures are read: emphasis marks gone and a run of
    blank lines collapsed (it carries nothing, and long runs are what a model
    that has lost its way produces)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _EMPHASIS.sub("", _BLANK_LINES.sub("\n\n", text))


def _prose_terms(text: str) -> list:
    """Content terms for the support ratio, with figures left intact.

    A comma between digits is a thousands separator, not a word boundary, and
    has to be read before the PROSE check as well as the fact check, because
    `_terms` splits on non-word characters and would otherwise turn one figure
    into two meaningless tokens ("85,000" as "85" and "000"). prose_form reads
    them the way the fact check does.
    """
    return _terms(fact_forms.prose_form(text))


@dataclass
class GroundingReport:
    grounded: bool = True
    cited_markers: tuple = ()
    invalid_markers: tuple = ()
    support_ratio: float = 0.0
    unsupported_terms: tuple = ()
    # Facts asserted by the answer that appear in none of the passages it
    # cited. This is the finding that matters: each entry is a number the
    # answer stated and the documents did not. They are comparison tokens
    # ("date:--08-15"); `facts.readable` says one the way a person would.
    unsupported_facts: tuple = ()
    failures: list = field(default_factory=list)
    # The answer as it should be shown: citations written as a group
    # ("[1, 2]", "[1-2]", "[Source 1]") split into one marker each. Empty until
    # validate() has run.
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.grounded and not self.failures


def extract_markers(text: str, available=None) -> tuple:
    """Markers the answer cites, in the order they first appear.

    `available` is the set of markers the context has: with it, a group such as
    "[0, 1]" that is not made of passages is not a citation (see
    facts.normalize_markers).
    """
    seen = []
    for match in MARKER.finditer(fact_forms.normalize_markers(text, available)):
        value = int(match.group(1))
        if value not in seen:
            seen.append(value)
    return tuple(seen)


def _normalize_fact(token: str) -> str:
    """A fact's comparable form.

    The same figure is written many ways across a corpus and an answer, and
    a check that missed those equivalences would reject correct answers for
    formatting:

        "75%"      "75 percent"   "75"        -> 75
        "85,000"   "85000"                    -> 85000
        "4.2."     "4.2"                      -> 4.2

    Trailing sentence punctuation is stripped and letters are lowered so
    "Rs1200" and "rs1200" agree; thousands separators were read already
    (facts.digit_commas). Identifiers keep their internal structure
    ("reg/2024/117") because there the structure IS the fact.
    """
    token = token.strip().strip(".,;:").lower()
    # A bare trailing separator left by the strip above.
    token = token.rstrip("./-:")
    # "2024-25" is "2024-2025".
    return fact_forms.canonical_range(token)


# A figure with a unit or currency stuck to it ("rs1200", "10km", "75%"),
# and a whole-hour time ("10:00"). The fact tokenizer keeps attached letters
# and the minutes, so "Rs1200" in a passage and "Rs 1200" or "1,200 rupees"
# in the answer compared as "rs1200" against "1200", and "10:00 p.m."
# against "10 PM" as "10:00" against "10" - correct answers rejected for
# formatting, which _normalize_fact exists to prevent. Identifiers with
# internal structure ("reg/2024/117") match neither pattern and still have
# to match exactly.
UNIT_FIGURE = re.compile(r"[a-z]*?(\d+(?:\.\d+)?)[a-z%]*")
WHOLE_HOUR = re.compile(r"(\d{1,2}):00")

# A figure a UNIT WORD is stuck to with a hyphen or a slash: "15-day", "8-week",
# "12-character", "Rs500/week", "85000/year", "15-day-window". It is its
# number: the passage says "15 days". Only a unit: any other letters after the
# dash are part of an identifier ("Form 16-A", "Section 12-B", "80-CCD") and stay
# whole, or "Form 16-B" would pass for "Form 16-A" and "Section 80-C" for "80
# percent". A letter prefix other than a currency is an identifier too
# ("ME204-lab").
_COMPOUND_UNIT = (
    r"(?:seconds?|minutes?|hours?|days?|weeks?|fortnights?|months?|quarters?"
    r"|semesters?|trimesters?|terms?|years?|sessions?|characters?|letters?"
    r"|digits?|words?|pages?|marks?|credits?|units?|rupees?|percent|points?"
    r"|classes|lectures?|attempts?|copies|copy|books?|members?|students?"
    r"|seats?|rooms?|beds?|floors?|km|kg|gb|mb|tb)"
)
GLUED_UNIT = re.compile(
    r"(?:rs|inr)?(\d+(?:\.\d+)?)[-/]" + _COMPOUND_UNIT + r"(?:[-/][a-z]+)*"
)
# "2nd/3rd": two figures.
GLUED_ORDINALS = re.compile(r"\d+(?:st|nd|rd|th)(?:/\d+(?:st|nd|rd|th))+")


def _numeric_core(fact: str):
    """The bare number in a figure, or None for anything more structured."""
    match = UNIT_FIGURE.fullmatch(fact) or WHOLE_HOUR.fullmatch(fact)
    return match.group(1) if match else None


def _split_glued(raw: str) -> list:
    token = raw.strip().strip(".,;:").lower()
    match = GLUED_UNIT.fullmatch(token)
    # "15-Sep" is a day and a month, not a number with a unit: its month is a
    # part of what it says, and the figure alone would be supported by any 15.
    if match and not any(
        word in fact_forms.MONTH_NUMBER for word in re.findall(r"[-/]([a-z]+)", token)
    ):
        return [match.group(1)]
    if GLUED_ORDINALS.fullmatch(token):
        return token.split("/")
    return [token]


def _facts(text: str) -> set:
    """Fact tokens in a piece of text, normalized."""
    found = set()
    for raw in FACT.findall(fact_forms.digit_commas(text)):
        for token in _split_glued(raw):
            if normalized := _normalize_fact(token):
                found.add(normalized)
    return found


def _answer_facts(answer_text: str) -> set:
    """Facts the answer asserts, excluding its own formatting.

    Citation markers are removed first - "[1]" is a reference, not a claim
    that the number one appears in the document - and so are markdown list
    ordinals and emphasis, which are numbering and typography rather than
    content. So are table references ("Table 2, row 3"): where a value is,
    not a value.
    """
    stripped = MARKER.sub(" ", _tidy(answer_text))
    stripped = text_rows.answer_without_references(stripped)
    stripped = ORDINAL_LINE.sub(" ", stripped)
    prepared = fact_forms.amounts(fact_forms.prepare(stripped))
    # A date the answer states is ONE fact, compared whole against the dates
    # the passages state: its day and its year are not facts of their own. So
    # is a clock time, which may be written two ways: it is a fact that is
    # supported by the time ("time:19:30") or by the figure as written, for a
    # passage that gives the hour only ("4 to 7 pm").
    remainder, dates = fact_forms.extract_dates(prepared)
    remainder, clock = fact_forms.extract_times(remainder)
    times = {f"{time}|{written}" for time, written in clock}
    # "65 - 75" and "65" + en dash + "75" are the range "65-75".
    remainder = fact_forms.range_dashes(remainder)
    return _facts(remainder) | dates | times


def _support_facts(text: str) -> set:
    """Facts the cited passages support, in every form they are written.

    The tokens as printed, plus each unambiguous date as a whole, each clock
    time as one, each amount as the number it is ("3 lakh" as 300000), the
    numerals behind number words ("seven" supports 7), the two years of a range
    ("2024-25" supports 2024 and 2025) and each range the text states
    ("range:65-75"). Only this side is widened: an answer's own wording is never
    turned into a figure it did not state.

    A labelled table row's reference ("Table 2, row 3:") supports nothing:
    "row 3" is where a value is, and a "3 credits" it matched is not in the
    document. The table's title and section in it still count.
    """
    cleaned = fact_forms.clean(text_rows.without_references(text))
    prepared = fact_forms.prepare(cleaned)
    # A dashed pair of figures is fused for the academic-year equivalences
    # ("2024-25"), and fused it lost both ends ("Batch 2020-2024"): the figures
    # as printed stay supported too.
    supported = _facts(prepared) | _facts(cleaned)
    amounted = fact_forms.amounts(prepared)
    if amounted != prepared:
        supported |= _facts(amounted)
    dates = fact_forms.extract_dates(amounted)[1]
    # A full date also supports an answer that leaves its year out.
    supported |= dates | fact_forms.year_less(dates)
    supported |= {time for time, _ in fact_forms.extract_times(amounted)[1]}
    supported |= fact_forms.number_words(prepared)
    supported |= fact_forms.range_pairs(fact_forms.amounts(cleaned))
    for fact in list(supported):
        supported |= fact_forms.range_years(fact)
    return supported


# A year, or an academic year, as a title states one.
YEAR_LIKE = re.compile(r"(?:19|20)\d{2}(?:-(?:19|20)\d{2})?")


def _title_facts(text: str) -> set:
    """The years a document's title states, as whole figures."""
    cleaned = fact_forms.clean(text)
    years = {
        fact
        for fact in _facts(fact_forms.prepare(cleaned)) | _facts(cleaned)
        if YEAR_LIKE.fullmatch(fact)
    }
    for fact in list(years):
        years |= fact_forms.range_years(fact)
    return years


def _fact_supported(fact: str, supported: set, cores: set) -> bool:
    """Whether a figure the answer states is one the passages state.

    The same figure, or the same bare number with a unit or currency stuck to
    it. A clock time is supported by the same time written another way, or by
    its figure as written. A RANGE ("2019-2021", "Rs 1500-2000") is supported
    by itself or by a passage that states that range ("from 2019 to 2021",
    "between 1500 and 2000"): both ends together, close together, in one
    passage. Two figures that merely both occur - "fail mark 40", "attendance
    75" - are not the range "40-75".
    """
    if fact in supported or _numeric_core(fact) in cores:
        return True
    if fact.startswith("time:"):
        time, _, written = fact.partition("|")
        return time in supported or (
            bool(written) and _fact_supported(written, supported, cores)
        )
    ends = fact_forms.range_parts(fact)
    return bool(ends) and f"range:{ends[0]}-{ends[1]}" in supported


# What the user typed, as far as an answer may repeat it: the clauses in which they
# state their OWN situation (facts.user_stated_clauses says what counts and why).
user_stated_text = fact_forms.user_stated_text


def validate(
    answer_text: str, context, abstained: bool = False, user_text: str = ""
) -> GroundingReport:
    """Check an answer against the context it was supposed to use.

    `user_text` is what the user typed (the question, and for a follow-up the
    turns that led to it). A figure the user gave about their own situation
    (user_stated_text) counts as supported, because they stated it and echoing
    it back ("at 60 percent attendance you would not be eligible") is not a
    hallucination. A figure they asked ABOUT does not. That is ALL it does: it
    never stands for a citation, an answer that cites nothing still fails, and
    repair_citations never sees it.
    """
    report = GroundingReport()
    available = set(context.by_marker())
    # "[1, 2]" and "[1-2]" are two citations, written as a group; "[0, 1]" is an
    # interval.
    answer_text = fact_forms.normalize_markers(answer_text, available)
    report.text = answer_text

    if abstained or is_abstention(answer_text):
        # An abstention makes no claims, so there is nothing to ground. It
        # is the correct output when evidence is missing, not a failure.
        report.grounded = True
        return report

    # A bracketed number that is not one of the context's markers but IS
    # printed in a retrieved passage ("Government Order [12]") is the
    # document's own reference, quoted - not a citation of passage 12.
    # Counting it made a verbatim, correct quote fail as ungrounded.
    #
    # Read the way a citation is read: a document's own "[2, 12]" is two
    # bracketed numbers, exactly as the answer's would be.
    quoted = {
        int(match.group(1))
        for passage in context.passages
        for match in MARKER.finditer(
            fact_forms.normalize_markers(passage.text, available)
        )
    }
    cited = tuple(
        marker
        for marker in extract_markers(answer_text, available)
        if marker in available or marker not in quoted
    )
    report.cited_markers = cited

    # A citation to a passage that does not exist is the cheapest hallucination
    # signal there is, and it stays fatal. It was once forgiven when it was
    # only off by one ([0], or the number after the last passage) - and that
    # let a whole uncited, unsourced sentence through with its marker quietly
    # deleted.
    invalid = tuple(marker for marker in cited if marker not in available)
    report.invalid_markers = invalid
    if invalid:
        report.grounded = False
        report.failures.append(
            f"answer cites passage(s) {list(invalid)} that were not in the "
            f"context (available: {sorted(available)})"
        )

    if not cited:
        report.grounded = False
        report.failures.append(
            "answer makes claims but cites no passage, so nothing in it can be checked"
        )
        return report

    # Everything below is measured only against the passages the answer
    # actually cited. Scoring against the whole context would let an answer
    # borrow credibility from evidence it never referenced.
    by_marker = context.by_marker()
    cited_passages = [by_marker[marker] for marker in cited if marker in by_marker]
    supporting = " ".join(passage.support_text for passage in cited_passages)
    titles = " ".join(passage.title_text for passage in cited_passages)

    # --- Check 3: every stated fact is in a cited passage ----------------
    # Read passage by passage: a range is two figures TOGETHER in one passage.
    supported_facts = set()
    for passage in cited_passages:
        supported_facts |= _support_facts(passage.support_text)
    # What the user told us about their own situation is theirs to repeat.
    own = user_stated_text(user_text)
    if own:
        supported_facts |= _support_facts(own)
    # A total, count, average, highest or lowest value that the cited tables'
    # own rows imply is not written anywhere, and is not an invention: it
    # is recomputed here, from the cells, rather than taken on the model's word.
    derived = tables.derived_figures(cited_passages)
    supported_facts |= derived
    stated_facts = _answer_facts(answer_text)
    supported_cores = {_numeric_core(fact) for fact in supported_facts} - {None}
    # The year a document's title states ("Fee Structure 2024-25") is on the
    # page the reader checks against, and is the year of every figure in it. It
    # counts as a whole figure and nothing else: not through the bare-number
    # equivalence above ("Hostel Rules v2" does not support "2 weeks"), and
    # not a title's other digits.
    supported_facts |= _title_facts(titles)

    unsupported_facts = sorted(
        fact
        for fact in stated_facts
        if not _fact_supported(fact, supported_facts, supported_cores)
    )
    report.unsupported_facts = tuple(unsupported_facts)

    if unsupported_facts:
        report.grounded = False
        shown = [fact_forms.readable(fact) for fact in unsupported_facts[:10]]
        report.failures.append(
            "answer states figure(s) that appear in none of the passages it "
            f"cites: {shown}"
        )

    # --- Check 4: the prose is drawn from those passages ------------------
    # A derived figure is evidence here too: "The total is 15,000" would
    # otherwise score as a sentence about words the table never uses.
    support_terms = (
        set(_prose_terms(supporting))
        | set(_prose_terms(titles))
        | set(_prose_terms(" ".join(derived)))
    )
    answer_terms = _prose_terms(MARKER.sub(" ", answer_text))

    if not answer_terms:
        report.support_ratio = 1.0
        return report

    unsupported = [term for term in answer_terms if term not in support_terms]
    report.support_ratio = round(1.0 - (len(unsupported) / len(answer_terms)), 4)
    report.unsupported_terms = tuple(sorted(set(unsupported))[:20])

    floor = getattr(settings, "RAG_MIN_SUPPORT_RATIO", None)
    if floor is None:
        floor = MIN_SUPPORT_RATIO

    if report.support_ratio < floor:
        report.grounded = False
        report.failures.append(
            f"only {report.support_ratio:.0%} of the answer's content words "
            f"appear in the passages it cites (minimum {floor:.0%}), which "
            f"suggests it is answering from something other than the evidence"
        )

    return report


def citations_for(answer_text: str, context) -> list:
    """Resolve an answer's markers to full citations, in cited order.

    A cited passage of labelled table rows names the rows the answer took
    from it ("Table 2, row 4") when the sentences citing it say which; see
    rag.tables.table_reference.
    """
    from .tables import table_reference

    by_marker = context.by_marker()
    citations = []

    for marker in extract_markers(answer_text, set(by_marker)):
        passage = by_marker.get(marker)
        if passage is None:
            continue
        citation = passage.citation
        citation["marker"] = marker
        citation["relevance"] = passage.relevance
        if "table" in citation:
            statement = _statement_citing(answer_text, marker)
            reference = table_reference(passage.chunk, statement)
            if reference is not None:
                citation["table"] = reference
        citations.append(citation)

    return citations


def _statement_citing(answer_text: str, marker: int) -> str:
    """The sentences of an answer that carry `[marker]`, joined."""
    token = f"[{marker}]"
    return " ".join(
        # The markers themselves are not figures the answer states.
        _MARKER_TOKEN.sub(" ", sentence)
        for sentence in _SENTENCE_BREAK.split(answer_text or "")
        if token in sentence
    )


# At most this many figures are re-attributed in one answer. A repair that
# needed more than two is an answer that was not written from what it cites.
MAX_REPAIRS = 2

# Only this many unsupported figures are looked at: an answer with more than
# MAX_REPAIRS of them cannot be repaired into a grounded one, and each is
# work (a pass over every uncited passage).
MAX_FIGURES_CONSIDERED = 20

# A full stop that ends a sentence is followed by whitespace; the one inside
# "B.Tech", "3.5" or "15.03.2025" is not. A line break ends one too.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKER_TOKEN = re.compile(r"\[\d{1,3}\]")

# What a clause and the passage it is attributed to must share besides the
# figure: two words, so the clause is about what the passage is about and not
# merely a number that happens to be in it.
MIN_SHARED_WORDS = 2

# The words beside a figure that say what it counts: this many either side.
FIGURE_NEIGHBOURS = 2
_WORD = re.compile(
    r"[^\W_]+(?:[.,:/-][^\W_]+)*|"
    r"[%\N{INDIAN RUPEE SIGN}\N{RUPEE SIGN}$\N{EURO SIGN}\N{POUND SIGN}]"
)
_FILLER = frozenset(
    "a an the of is are was were be been being to in on at for and or by with "
    "from as it its that this than not no".split()
)
# Rs, Rs., INR, the rupee sign and "rupees" are one unit; so are % and percent.
_UNIT_WORD = {
    "rs": "rupee",
    "inr": "rupee",
    "\N{INDIAN RUPEE SIGN}": "rupee",
    "\N{RUPEE SIGN}": "rupee",
    "%": "percent",
}


def _distinctive(fact: str) -> bool:
    """A figure specific enough that finding it in one passage means something.

    "2" or "10" turn up in most passages; a date, or a figure of three or more
    digits, does not. Only these are ever attributed to a passage.
    """
    return fact.startswith("date:") or len(re.sub(r"\D", "", fact)) >= 3


def _words(text: str) -> set:
    return {term for term in _prose_terms(text) if not any(c.isdigit() for c in term)}


def _beside(word: str) -> str:
    word = _UNIT_WORD.get(word, word)
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


_BEFORE_A_UNIT = _FILLER | {"per"}


def _figure_reading(text: str, fact: str, depth: int):
    """What a figure is ABOUT, as the words around it say, or None for a kind of
    figure this does not read (a time, a range): those are never repaired.

    (units, subject). `units` is what it counts: the word right after it, or the
    currency mark right before it ("rupee", "day", "percent", "seat"). `subject`
    is the `depth` content words nearest before it ("hostel fee", "late fine").
    "200 rupees" and "200 days" differ in unit, "200 students" and "200 seats"
    too, and "the exam fee is 4500" and "a bus pass costs 4500" in subject; a
    clause and a passage about the same thing share a subject.
    """
    if fact.startswith("time:") or fact_forms.range_parts(fact):
        return None
    prepared = fact_forms.amounts(fact_forms.prepare(text))
    inline, _ = fact_forms.extract_dates(prepared, inline=True)
    words = _WORD.findall(inline.lower())
    is_date = fact.startswith("date:")

    if is_date:
        code = fact[len("date:") :].replace("-", "").replace(":", "")
        year_less = fact.startswith("date:--")

        def is_the_figure(word):
            if not word.startswith("date"):
                return False
            if year_less:
                return word.endswith(code) and word[len("date") :].isdigit()
            return word == "date" + code

    else:
        core = _numeric_core(fact)

        def is_the_figure(word):
            return any(
                found == fact or (core is not None and _numeric_core(found) == core)
                for found in _facts(word)
            )

    units, subject = set(), set()
    for index, word in enumerate(words):
        if not is_the_figure(word):
            continue
        if not is_date:
            if index and words[index - 1] in _UNIT_WORD:
                units.add(_beside(words[index - 1]))
            for after in words[index + 1 : index + 1 + FIGURE_NEIGHBOURS]:
                after = _beside(after)
                if after in _BEFORE_A_UNIT:
                    continue
                if len(after) > 1 and not any(c.isdigit() for c in after):
                    units.add(after)
                break
        taken = 0
        for before in reversed(words[max(0, index - 3 * depth) : index]):
            before = _beside(before)
            if (
                before in _UNIT_WORD
                or before in _BEFORE_A_UNIT
                or len(before) <= 1
                or any(c.isdigit() for c in before)
            ):
                continue
            subject.add(before)
            taken += 1
            if taken == depth:
                break
    return units, subject


def repair_citations(report: GroundingReport, context):
    """(the answer with a marker added where one was missing, [(figure, marker)]).

    An answer that cites [1] for one clause and states a figure in a LATER
    clause that is written in [2], and in no other passage, did not invent it:
    it forgot to cite [2], and was discarded whole for it. The marker is added
    to that clause. It is deliberately narrow, because what it excuses is a
    figure the answer's own citations do not support:

    - Only a figure stated AFTER the last marker of its sentence. A figure that
      a marker follows ("The B.Tech hostel fee is Rs 85,000 [1]") is the model's
      own attribution, and if [1] does not state it the model mixed passages up:
      that is the failure the check exists to catch, and it is not repaired.
    - Only from a passage's own text, in exactly one uncited passage, and only
      when the clause shares at least two words with it.
    - Only when the clause and the passage are about the same thing, and count
      the same thing. A unit the clause states (the word right after the figure,
      or the currency mark right before it) must be the passage's: "200 rupees"
      and "Rs 200", not "200 days", nor "200 students" against "200 seats". And the
      content words just before the figure must overlap ("the hostel fee is
      42000" and "The hostel fee is 42000 per year", not "the exam fee is 4500
      rupees" against a bus pass that costs 4500 rupees). A time or a range is
      never repaired.
    - Never a table row, never for an answer that cites nothing valid, at most
      twice, and the result is checked again from scratch.

    Only ever a passage's own text: what the user typed (validate's `user_text`)
    is not here, because a figure the user typed needs no attribution.
    """
    text = report.text
    cited = set(report.cited_markers)
    if not report.unsupported_facts or not cited or report.invalid_markers:
        return text, []

    spans = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))

    # Worked out once each: the facts a passage states, the facts a clause
    # states, the words of a passage. Left to the loop they were recomputed for
    # every figure, and the cost grew with figures times passages.
    stated = {}
    clauses = {}
    owner_words = {}

    def stated_in(passage, fact):
        if passage.marker not in stated:
            supported = _support_facts(passage.text)
            cores = {_numeric_core(f) for f in supported} - {None}
            stated[passage.marker] = (supported, cores)
        supported, cores = stated[passage.marker]
        return _fact_supported(fact, supported, cores)

    def clause_of(begin, finish):
        if (begin, finish) not in clauses:
            sentence = text[begin:finish]
            markers = list(_MARKER_TOKEN.finditer(sentence))
            clause_begin = begin + (markers[-1].end() if markers else 0)
            clause = text[clause_begin:finish]
            clauses[begin, finish] = (clause_begin, clause, _answer_facts(clause))
        return clauses[begin, finish]

    repairs = []
    insertions = {}
    for fact in report.unsupported_facts[:MAX_FIGURES_CONSIDERED]:
        if len(repairs) >= MAX_REPAIRS:
            break
        if not _distinctive(fact):
            continue
        owners = [
            passage
            for passage in context.passages
            if passage.marker not in cited and stated_in(passage, fact)
        ]
        if len(owners) != 1:
            continue
        owner = owners[0]
        if owner.marker not in owner_words:
            owner_words[owner.marker] = _words(owner.text)
        owner_reading = False  # not read yet; None is a kind it cannot read

        for begin, finish in spans:
            if text[begin:finish].count("|") >= 2:
                continue
            clause_begin, clause, clause_facts = clause_of(begin, finish)
            if fact not in clause_facts:
                continue
            if len(_words(clause) & owner_words[owner.marker]) < MIN_SHARED_WORDS:
                continue
            # A date is told apart by the verb beside it ("closes on"), so two
            # words each side; an amount by what it is the amount OF, which a
            # passage may say further back ("a lost bus pass is replaced on
            # payment of 200 rupees"): three in the clause against six in it.
            is_date = fact.startswith("date:")
            if owner_reading is False:
                owner_reading = _figure_reading(owner.text, fact, 2 if is_date else 6)
            clause_reading = _figure_reading(clause, fact, 2 if is_date else 3)
            if clause_reading is None or owner_reading is None:
                continue
            clause_units, clause_subject = clause_reading
            owner_units, owner_subject = owner_reading
            # A unit the clause states must be the passage's; the clause and the
            # passage must be about the same thing.
            if clause_units and not (clause_units & owner_units):
                continue
            if not (clause_subject & owner_subject):
                continue
            position = clause_begin + len(clause.rstrip(".!?;:, \t\n"))
            insertions.setdefault(position, set()).add(owner.marker)
            repairs.append((fact, owner.marker))
            break

    for position in sorted(insertions, reverse=True):
        marks = "".join(f" [{marker}]" for marker in sorted(insertions[position]))
        text = text[:position] + marks + text[position:]
    return text, repairs


def readable(fact: str) -> str:
    """A figure, date or time the way a person writes it, for anything shown to a
    reader or a model: the tokens in `unsupported_facts` and in the pairs
    repair_citations returns are comparison forms ("date:2025-03-31")."""
    return fact_forms.readable(fact)


def correction_for(report: GroundingReport) -> str:
    """What to tell a model whose answer stated figures its passages lack.

    The figures are named as a person writes them ("15 August", "31 April 2025
    (not a real date)"), not as the check compares them: a model cannot map
    "--08-15" back to what it wrote.
    """
    shown = [fact_forms.readable(fact) for fact in report.unsupported_facts[:8]]
    return (
        "\n\nYour previous answer was rejected by an automated check: it stated "
        f"{', '.join(shown)}, which the passages you cited do not contain. "
        "Answer again. Write every figure and date exactly as a passage writes "
        "it, cite the passage each one comes from, and if the passages do not "
        "give a figure, say so instead of stating one."
    )
