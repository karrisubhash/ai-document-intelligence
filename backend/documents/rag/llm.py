"""The LLM gateway: model abstraction, and the one baseline that needs none.

Section 26 requires providers to stay replaceable behind a gateway
interface, and section 18 requires timeouts and fallback to be the gateway's
problem rather than every caller's. This module is that boundary.

The bundled `ExtractiveAnswerer` does not generate. It selects the sentences
from the retrieved passages that best answer the question and returns them
verbatim with their markers. That is a real, useful answerer for a document
platform - "show me the sentence that says this" is most of what people
actually want - and it has a property no generative model has: it cannot
hallucinate, because every character it emits was copied from a passage the
caller was authorized to read.

It is a baseline, not a destination. It cannot synthesise across passages,
resolve pronouns or rephrase. `generative = False` says so, and swapping in
a real provider is one setting.
"""

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from . import quoting

ABSTENTION_TEXT = "I couldn't find enough information in the provided documents."


def _normalise_reply(text: str) -> str:
    text = (text or "").replace("’", "'").replace("‘", "'")
    text = text.strip().strip("\"'“”").strip().rstrip(".").strip()
    return " ".join(text.split()).lower()


def is_abstention(text: str) -> bool:
    """Whether a reply is the abstention sentence, as a model writes it.

    Compared exactly, a reply that reproduced the quotes the system prompt
    shows the sentence in, or wrote "couldn’t" with a typographic
    apostrophe, did not count: carrying no markers, it then failed
    grounding and was reported as UNGROUNDED - the hallucination signal -
    instead of an honest abstention.
    """
    return _normalise_reply(text) == _normalise_reply(ABSTENTION_TEXT)


# A model told to reply with one exact sentence often replies with the same
# message in its own words: "I couldn't find the PhD fee in these documents.
# They only list B.Tech and M.Tech." That is a refusal, but is_abstention()
# is exact, and the reply has no citation, so it failed grounding and was
# stored as UNGROUNDED - the hallucination signal - and, being neither
# NO_RELEVANT_PASSAGES nor MODEL_ABSTAINED, was kept away from the
# general-knowledge reply it should have had.
_DECLINE = re.compile(
    r"\b(?:(?:could\s*n[o']t|can\s*n[o']t|cannot|can't|unable\s+to|"
    r"(?:did|do|does)\s*n[o']t|didn't|don't|doesn't)\s+"
    r"(?:find|locate|see|contain|mention|cover|include|provide|specify|state|"
    r"say|have|address|answer)"
    r"|no\s+(?:relevant\s+|such\s+|specific\s+)?(?:information|mention|details?|"
    r"passages?|data)"
    r"|not\s+(?:covered|mentioned|specified|stated|available|provided|found)"
    r"\s+in)\b",
    re.I,
)
_CITATION = re.compile(r"\[\d{1,3}\]")
_HAS_FIGURE = re.compile(r"\d")


def declines_to_answer(text: str) -> bool:
    """Whether a reply refuses in its own words rather than answering.

    Only a reply with no citation and no figure qualifies: it says it found
    nothing and offers nothing a reader could act on. One that cites a
    passage or states a number is an answer - possibly a wrong one - and
    still goes through grounding, which is how a wrong answer is caught.
    """
    text = (text or "").replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    text = text.replace("\N{LEFT SINGLE QUOTATION MARK}", "'")
    if not text.strip() or _CITATION.search(text) or _HAS_FIGURE.search(text):
        return False
    return bool(_DECLINE.search(text))


# Sentence splitting good enough for institutional prose. Deliberately not a
# full NLP sentence tokenizer - the failure mode of splitting "Clause 4.2"
# badly is a slightly long quote, which is harmless here.
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

# The separators a table row can use, matching the detector's set. Used only
# to tell a header row from a data row, never to re-parse a table - the
# parsed cells already live on the stored DocumentTable.
CELL_SEPARATOR = re.compile(r"\t+|\s{2,}|\s*\|\s*")


@dataclass
class LLMResponse:
    text: str
    abstained: bool = False
    cited_markers: tuple = ()
    model_name: str = ""
    model_version: str = ""
    generative: bool = False
    prompt_characters: int = 0
    completion_characters: int = 0
    # Populated by providers that report real usage; the extractive baseline
    # has no tokens to count and leaves these at zero rather than guessing.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    metadata: dict = field(default_factory=dict)
    # What the provider told us, kept rather than thrown away (see
    # documents.rag.gateway). `model_name` above is the ANSWERER ("openai-
    # compatible", a bounded metrics label); these say which model at which
    # provider actually wrote the text.
    model: str = ""
    provider: str = ""
    finish_reason: str = ""
    # The link this response stands in for ("provider/model"): the first in
    # the chain when a later one answered, or when every one failed and the
    # passages were quoted instead. Empty when the first choice answered.
    fell_back_from: str = ""
    # Whether what was sent may be used for training by whoever received it:
    # the answering provider's free tier, or any provider tried on the way.
    may_train_on_input: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """What the RAG engine needs from any answerer."""

    name: str
    version: str
    generative: bool

    def answer(self, *, question: str, context, prompt: str) -> LLMResponse: ...


def _looks_like_a_table_row(line: str) -> bool:
    """A line whose cells are separated by tabs, pipes or a column gap."""
    return len([cell for cell in CELL_SEPARATOR.split(line.strip()) if cell]) >= 2


def _unwrap(text: str) -> list:
    """Rejoin lines that a right margin split, and nothing else.

    The test for a continuation is that the next line STARTS LOWERCASE.
    That is the conservative half of the classic hard-wrap heuristic, and
    the conservative half is the one worth having here:

        "Clause 4.2 Attendance"              <- heading, next line is "A
        "A student must maintain ..."           student" - NOT joined
        "... in each course to"              <- next line is "be eligible"
        "be eligible for the examination."      - joined

    Under-joining costs a slightly short quote. Over-joining would weld a
    heading onto a clause, or two unrelated clauses together, and present
    the result as one sentence from the document.

    Table rows are never joined on either side: their newlines are the row
    separators, and merging two rows would produce a row that is not in the
    table at all.
    """
    lines = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        previous = lines[-1] if lines else ""
        continues = (
            previous
            and line[:1].islower()
            and not previous.rstrip().endswith((".", "!", "?", ":", ";"))
            and not _looks_like_a_table_row(line)
            and not _looks_like_a_table_row(previous)
        )

        if continues:
            # A single space where the margin put a newline. The characters
            # are the document's; only that one whitespace is normalized,
            # and the citation still points at the full span on the page.
            lines[-1] = f"{previous} {line}"
        else:
            lines.append(line)

    return lines


# A bracketed number in the DOCUMENT ("as per Government Order [12]",
# "clause [2]") is the citation syntax this system uses for its own markers.
# Quoted verbatim, it was read back as a citation: [12] failed grounding as
# a passage that was never in the context, and [2] silently attached an
# unrelated passage. Parentheses keep the reference readable and the figure
# checkable, and leave square brackets to the markers alone.
#
# A group ("earlier studies [2, 12]") is read as two citations by the check
# that reads an answer's own "[1, 2]" as two, so it is neutralised too.
DOCUMENT_BRACKET = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]")


def _neutralise_brackets(text: str) -> str:
    return DOCUMENT_BRACKET.sub(r"(\1)", text)


@dataclass
class _Candidate:
    """One sentence that could be quoted, and why it was a candidate."""

    score: float
    coverage: float
    passage: object
    sentence: str
    # The terms the user asked about that this sentence covers (a table row's
    # include its column labels).
    matched: frozenset
    # A title or clause heading rather than a sentence (quoting.is_heading),
    # and whether it is set behind the sentences that qualify: a heading that
    # states the year or date a question asks for is not.
    heading: bool
    demoted: bool
    # The first sentence of a chunk that begins mid-sentence, and the last of a
    # chunk that stops mid-sentence: both are cut by a chunk boundary.
    leading: bool
    trailing: bool

    @property
    def order(self):
        # Best first; a shorter sentence wins a tie, because a tighter quote
        # that says the same thing is a better answer.
        return (-self.score, len(self.sentence), self.passage.marker)


class ExtractiveAnswerer:
    """Answers by quoting the passages, never by generating new text."""

    name = "extractive-answerer"
    version = "0.1.0"
    generative = False

    # A sentence must address at least this much of the question to be
    # quoted. Below it, returning the sentence would be presenting a weak
    # lexical coincidence as an answer.
    MIN_COVERAGE = 0.34

    # ...and a ratio alone is not enough, because it is blind to how many
    # terms the ratio is over. On a two-term question, matching one common
    # word scores 50% and sails past the threshold:
    #
    #   "What is the parking policy?"  ->  "TRANSPORT POLICY"
    #
    # which is a document heading quoted as an answer on the strength of
    # the word "policy". Requiring two matched terms as well as the ratio
    # costs nothing on real questions - "attendance minimum", "hostel
    # curfew" and "B.Tech CSE" all match on two or more - and removes the
    # single-common-word coincidence entirely.
    #
    # Skipped when the question itself has only one content term, where
    # one match is all there is to find.
    MIN_MATCHED_TERMS = 2

    # How many sentences an answer may quote. More than this stops being an
    # answer and becomes a search result page.
    MAX_SENTENCES = 3

    # A "how much" or "when" question is answered by a sentence with a figure
    # or a date in it. Added to the coverage of a sentence that has one, so it
    # settles a tie and a near tie and never outweighs a clearly better match.
    FIGURE_BONUS = 0.12

    # The largest table this answerer will quote whole when a question
    # matches its column labels rather than any one row. Above it, the
    # honest output is an abstention: a 120-row table pasted into an answer
    # is a wall of text that makes the reader do the retrieval.
    TABLE_QUOTE_LIMIT = 800

    def answer(self, *, question: str, context, prompt: str) -> LLMResponse:
        query_terms = quoting.quote_terms(question)

        if not query_terms or context.is_empty:
            return self._abstain(prompt)

        # On a follow-up, the terms the user actually typed. A sentence that
        # addresses none of them is not an answer to THIS question, however
        # well it matches the earlier turns' vocabulary. Measured: "What
        # about condonation?" after an attendance question quoted the
        # attendance clause back, because the borrowed terms outnumbered
        # the user's one.
        focus_text = getattr(context, "focus", "") or ""
        focus_terms = quoting.quote_terms(focus_text)
        asked = focus_terms or query_terms

        # A passage that is trying to manipulate the system stays in the
        # context - it was retrieved, it is reported, and search still finds
        # it, so nothing is hidden. But it is not quoted as an ANSWER. Text
        # that is trying to manipulate the system is not a trustworthy source
        # of fact, and emitting it beside a real answer launders it into
        # looking like one: a reader skimming "...75 percent [1] ...10
        # percent [2]" is worse off than a reader who saw only the answer and
        # a warning.
        usable = [p for p in context.passages if not p.suspected_injection]
        skipped_hostile = len(context.passages) - len(usable)

        # Something the question names that no passage mentions: a programme
        # the fee table does not list, a year no document covers. A ranker
        # that counts shared words cannot see it; a reader sees it at once.
        lacking = quoting.names_what_the_passages_lack(focus_text or question, usable)
        if lacking:
            return self._abstain(prompt, hostile=skipped_hostile, reason=lacking)

        wants_figure = quoting.wants_figure(focus_text or question)
        date_question = quoting.wants_date(focus_text or question)

        eligible = []
        partial = []

        for passage in usable:
            header_terms = quoting.quote_terms(
                " ".join(getattr(passage, "table_header", ()) or ())
            )
            sentences = self._sentences(passage.text)
            fragment_start = passage.text.lstrip()[:1].islower()

            for position, sentence in enumerate(sentences):
                own_terms = quoting.quote_terms(sentence)
                if not own_terms:
                    continue

                looks_like_a_row = _looks_like_a_table_row(sentence)
                is_header = self._is_header_row(passage, sentence)

                # A data row means what its column labels say it means: "B.Tech
                # ECE 82000 2500 60000" is a TUITION figure only under
                # "Tuition". Matched without them, a question that named the
                # column ("tuition") matched nothing in the row and the
                # answer was a document title. A row is still only a candidate
                # if the question named something IN the row: a question that
                # names only labels is answered by the table, below.
                is_row = bool(header_terms) and looks_like_a_row and not is_header
                terms = own_terms | header_terms if is_row else own_terms
                if is_row and not (own_terms & asked):
                    continue

                matched = query_terms & terms
                coverage = len(matched) / len(query_terms)
                asked_matched = frozenset(asked & terms)

                # The minimum-matched-terms rule applies to the question
                # actually being answered: on a follow-up, the user's own
                # words. Applied to the widened query, "What about weekends?"
                # (one term of its own) refused "On weekends, students may
                # sign back in by 11 pm" for not ALSO repeating a term from
                # the earlier turn.
                if (
                    len(asked) >= self.MIN_MATCHED_TERMS
                    and len(asked_matched) < self.MIN_MATCHED_TERMS
                ):
                    continue

                bonus = (
                    self.FIGURE_BONUS
                    if wants_figure and quoting.has_figure(sentence)
                    else 0.0
                )

                if focus_terms:
                    focused = focus_terms & terms
                    if not focused:
                        continue
                    # Ranked by how much of what the user asked it covers
                    # first; the widened question only breaks ties. The
                    # coverage floor is then applied to the user's words,
                    # which is the question actually being answered.
                    focus_coverage = len(focused) / len(focus_terms)
                    if focus_coverage < self.MIN_COVERAGE:
                        continue
                    score = focus_coverage + coverage / 10 + bonus
                    meets_floor = True
                else:
                    score = coverage + bonus
                    meets_floor = coverage >= self.MIN_COVERAGE

                heading = quoting.is_heading(sentence, is_row=looks_like_a_row)
                candidate = _Candidate(
                    score=score,
                    coverage=coverage,
                    passage=passage,
                    sentence=sentence,
                    matched=asked_matched,
                    heading=heading,
                    demoted=heading
                    and not (date_question and quoting.heading_has_figure(sentence)),
                    leading=position == 0 and fragment_start,
                    trailing=position == len(sentences) - 1
                    and quoting.ends_cut(sentence),
                )
                # A sentence that matches enough terms but not enough of the
                # WHOLE question may still answer one part of it (below).
                (eligible if meets_floor else partial).append(candidate)

        if not eligible:
            return self._abstain(prompt, hostile=skipped_hostile)

        # Sorted here rather than after the header filter below, because
        # that branch reads eligible[0] as "the best match" and on an
        # unsorted list that is merely "the first one found".
        eligible.sort(key=lambda candidate: candidate.order)

        # A header row is labels, not data, and quoting one as an answer is
        # the most confidently useless thing this answerer can do. Observed
        # live, both ways round:
        #
        #   "What is the hostel fee for B.Tech MECH?" -> the right row, and
        #   then the header row appended beside it as a second "answer".
        #
        #   "What is the exam fee?" -> the header row ALONE, because it is
        #   the only text in the table containing the words "exam fee". A
        #   reader is told the table has an exam fee column and never told
        #   what it says.
        #
        # So a header is never quoted on its own. When the question matched
        # only labels, the useful answer is the table itself: the labels
        # with the rows underneath them, verbatim, so the value can be read
        # off. Bounded by size, because a 120-row table is not an answer.
        data = [
            candidate
            for candidate in eligible
            if not self._is_header_row(candidate.passage, candidate.sentence)
        ]

        if not data:
            best = eligible[0].passage
            if len(best.text) <= self.TABLE_QUOTE_LIMIT:
                text = f"{_neutralise_brackets(best.text.strip())} [{best.marker}]"
                return LLMResponse(
                    text=text,
                    abstained=False,
                    cited_markers=(best.marker,),
                    model_name=self.name,
                    model_version=self.version,
                    generative=self.generative,
                    prompt_characters=len(prompt),
                    completion_characters=len(text),
                    metadata={
                        "quoted_sentences": 1,
                        "best_coverage": round(eligible[0].coverage, 4),
                        "strategy": "whole table quoted: the question matched "
                        "the column labels rather than any one row",
                        "passages_skipped_as_hostile": skipped_hostile,
                    },
                )
            return self._abstain(
                prompt,
                hostile=skipped_hostile,
                reason=(
                    "the question matched a table's column labels but no "
                    "single row, and the table is too large to quote whole"
                ),
            )

        # A title or a clause heading is quoted only when nothing else
        # qualifies, and then only if it can be the answer: a heading states
        # no figure and no date, so it cannot answer "how much" or "when" -
        # "By which date must I sign up?" was answered with the name of the
        # office. The exception is a question for a date or a year, and a
        # heading that states one: "Subject: Campus bus services for the
        # academic year 2024-25" IS the answer to "which year".
        pool = [candidate for candidate in data if not candidate.demoted]
        if not pool:
            pool = data
            if wants_figure and not any(
                quoting.has_figure(candidate.sentence) for candidate in pool
            ):
                return self._abstain(
                    prompt,
                    hostile=skipped_hostile,
                    reason=(
                        "only a title or heading matched, and it states no "
                        "figure or date"
                    ),
                )

        def key_of(candidate):
            return " ".join(candidate.sentence.lower().split())

        first = pool[0]
        chosen = [first]
        seen = {key_of(first)}

        # Equally good answers are all quoted: two rows that both say CSE are
        # the same answer to "the fee for CSE", and picking one would hide the
        # other. A sentence that matched LESS than the best is not one.
        for candidate in pool[1:]:
            if len(chosen) >= self.MAX_SENTENCES:
                break
            if (
                candidate.matched == first.matched
                and abs(candidate.score - first.score) < 1e-9
                and key_of(candidate) not in seen
            ):
                chosen.append(candidate)
                seen.add(key_of(candidate))

        # What the question still asks. A two-part question matches each
        # part's sentence on only HALF its terms, under the floor measured
        # over the whole question, so the second part was dropped. After the
        # best sentence, another is added if it says something the chosen ones
        # do not:
        #
        # - a sentence that is itself a good match is added when it covers a
        #   term not yet covered ("Condonation granted for examination
        #   purposes does not restore a scholarship" beside the scholarship's
        #   attendance rule, for a question about both);
        # - one that matched too little of the WHOLE question to qualify alone
        #   is added only if it covers two new terms and a third of what is
        #   left (the other half of a two-part question).
        #
        # A sentence that covers nothing new is not added, which keeps a
        # single-topic answer from being padded with the same clause twice.
        if not first.demoted:
            covered = set().union(*(candidate.matched for candidate in chosen))
            good = [candidate for candidate in pool if not candidate.demoted]
            partial_ok = [
                candidate
                for candidate in partial
                if not candidate.demoted
                and not self._is_header_row(candidate.passage, candidate.sentence)
            ]
            # "Last day" and "deadline" say what KIND of answer is wanted, not
            # what it is about: a sentence that shares only those words with a
            # question ("All fees are payable before the last working day of
            # July" for "the last day to submit applications for aid") is not
            # a second part of it.
            form_terms = quoting.FORM_TERMS if date_question else frozenset()
            while len(chosen) < self.MAX_SENTENCES:
                uncovered = asked - covered - form_terms
                if not uncovered:
                    break
                options = []
                for candidate in good:
                    gained = candidate.matched & uncovered
                    if gained and key_of(candidate) not in seen:
                        options.append((-len(gained), candidate.order, candidate))
                if not options:
                    for candidate in partial_ok:
                        gained = candidate.matched & uncovered
                        if (
                            len(gained) >= self.MIN_MATCHED_TERMS
                            and len(gained) / len(uncovered) >= self.MIN_COVERAGE
                            and key_of(candidate) not in seen
                        ):
                            options.append((-len(gained), candidate.order, candidate))
                if not options:
                    break
                options.sort(key=lambda option: option[:2])
                pick = options[0][2]
                chosen.append(pick)
                seen.add(key_of(pick))
                covered |= pick.matched

        parts = [
            self._quote(
                candidate.passage,
                candidate.sentence,
                leading=candidate.leading,
                trailing=candidate.trailing,
            )
            for candidate in chosen
        ]
        text = " ".join(parts)

        return LLMResponse(
            text=text,
            abstained=False,
            cited_markers=tuple(sorted({c.passage.marker for c in chosen})),
            model_name=self.name,
            model_version=self.version,
            generative=self.generative,
            prompt_characters=len(prompt),
            completion_characters=len(text),
            metadata={
                "quoted_sentences": len(chosen),
                "best_coverage": round(chosen[0].coverage, 4),
                "strategy": "verbatim extraction",
                "passages_skipped_as_hostile": skipped_hostile,
            },
        )

    @staticmethod
    def _is_header_row(passage, sentence: str) -> bool:
        """Whether this sentence IS the table's header row.

        Compared cell by cell rather than by string equality, because the
        sentence comes from the page text - with whatever separator the
        document used - while the header is the parsed cells.
        """
        header = getattr(passage, "table_header", ())
        if not header:
            return False

        cells = [
            " ".join(cell.split())
            for cell in CELL_SEPARATOR.split(sentence.strip())
            if cell.strip()
        ]
        return cells == list(header)

    @staticmethod
    def _quote(
        passage, sentence: str, *, leading: bool = False, trailing: bool = False
    ) -> str:
        """One quoted sentence with its marker, and its columns if it is a
        table row.

        "B.Tech CSE 85000 2500 [1]" is not an answer to "what is the tuition
        for CSE" - it is three numbers and a hope that the reader guesses
        which one was asked for. The column labels are what make the middle
        figure a tuition fee.

        They are appended as a labelled aside rather than woven into the
        quote, so the verbatim text stays verbatim and it is visible that
        the labels come from the table's header row rather than from the
        sentence. Those labels are document content too: the detector
        verified every cell against the page before the table was stored.

        A sentence a chunk boundary cut is marked with an ellipsis where it was
        cut, so the reader is told it continues, rather than shown "f member is
        entitled to 12 days" as if that were how the document begins.
        """
        text = sentence.strip()
        start_cut = False
        if leading:
            text, start_cut = quoting.trim_cut_start(text)

        shown = _neutralise_brackets(text)
        if start_cut:
            shown = "\N{HORIZONTAL ELLIPSIS}" + shown
        if trailing:
            shown += "\N{HORIZONTAL ELLIPSIS}"
        quoted = f"{shown} [{passage.marker}]"

        header = getattr(passage, "table_header", ())
        if not header:
            return quoted

        # Nothing to add when the quoted row IS the header.
        row = " ".join(sentence.split())
        if all(cell in row for cell in header):
            return quoted

        return f"{quoted} (table columns: {' | '.join(header)})"

    def _abstain(self, prompt: str, hostile: int = 0, reason: str = "") -> LLMResponse:
        return LLMResponse(
            text=ABSTENTION_TEXT,
            abstained=True,
            cited_markers=(),
            model_name=self.name,
            model_version=self.version,
            generative=self.generative,
            prompt_characters=len(prompt),
            completion_characters=len(ABSTENTION_TEXT),
            metadata={
                "reason": reason or "no passage sentence addressed the question",
                "passages_skipped_as_hostile": hostile,
            },
        )

    @staticmethod
    def _sentences(text: str) -> list:
        """Sentences, with hard-wrapped lines rejoined first.

        Newlines really are clause boundaries in this corpus - institutional
        documents put one clause per line far more often than they write
        flowing prose - so they are treated as sentence ends. But a document
        wrapped at the right margin puts newlines INSIDE sentences too, and
        treating those as boundaries truncates the answer mid-thought:

            A student must maintain a minimum of 75 percent attendance in
            each course to
            be eligible for the end-semester examination.

        asked "what is the attendance minimum?" answered
        "...in each course to", which reads as a broken system rather than
        a short one. Observed live, on a plain hard-wrapped regulation.

        _unwrap rejoins continuation lines before splitting. It is
        deliberately conservative: only a line beginning with a lowercase
        letter continues the one above it, and table rows are never joined.
        """
        pieces = []
        for line in _unwrap(text):
            pieces.extend(part for part in SENTENCE.split(line) if part.strip())
        return pieces
