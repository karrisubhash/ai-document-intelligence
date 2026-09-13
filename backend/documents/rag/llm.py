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

from .reranking import _terms

ABSTENTION_TEXT = (
    "I couldn't find enough information in the provided documents."
)

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

    # The largest table this answerer will quote whole when a question
    # matches its column labels rather than any one row. Above it, the
    # honest output is an abstention: a 120-row table pasted into an answer
    # is a wall of text that makes the reader do the retrieval.
    TABLE_QUOTE_LIMIT = 800

    def answer(self, *, question: str, context, prompt: str) -> LLMResponse:
        query_terms = set(_terms(question))

        if not query_terms or context.is_empty:
            return self._abstain(prompt)

        scored = []
        skipped_hostile = 0

        for passage in context.passages:
            if passage.suspected_injection:
                # The passage stays in the context - it was retrieved, it is
                # reported, and search still finds it, so nothing is hidden.
                # But it is not quoted as an ANSWER. Text that is trying to
                # manipulate the system is not a trustworthy source of fact,
                # and emitting it beside a real answer launders it into
                # looking like one: a reader skimming
                # "...75 percent [1] ...10 percent [2]" is worse off than a
                # reader who saw only the answer and a warning.
                skipped_hostile += 1
                continue

            for sentence in self._sentences(passage.text):
                sentence_terms = set(_terms(sentence))
                if not sentence_terms:
                    continue
                matched = query_terms & sentence_terms
                coverage = len(matched) / len(query_terms)

                if len(query_terms) >= self.MIN_MATCHED_TERMS and len(
                    matched
                ) < self.MIN_MATCHED_TERMS:
                    continue

                if coverage >= self.MIN_COVERAGE:
                    scored.append((coverage, passage, sentence))

        if not scored:
            return self._abstain(prompt, hostile=skipped_hostile)

        # Sorted here rather than after the header filter below, because
        # that branch reads scored[0] as "the best match" and on an
        # unsorted list that is merely "the first one found".
        #
        # Best coverage first; shorter sentences win ties because a tighter
        # quote that says the same thing is a better answer.
        scored.sort(key=lambda item: (-item[0], len(item[2]), item[1].marker))

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
        data = [item for item in scored if not self._is_header_row(item[1], item[2])]

        if not data:
            best = scored[0][1]
            if len(best.text) <= self.TABLE_QUOTE_LIMIT:
                text = f"{best.text.strip()} [{best.marker}]"
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
                        "best_coverage": round(scored[0][0], 4),
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

        scored = data

        chosen = []
        seen = set()
        for coverage, passage, sentence in scored:
            key = " ".join(sentence.lower().split())
            if key in seen:
                continue
            seen.add(key)
            chosen.append((passage, sentence, coverage))
            if len(chosen) >= self.MAX_SENTENCES:
                break

        parts = [
            self._quote(passage, sentence) for passage, sentence, _ in chosen
        ]
        text = " ".join(parts)

        return LLMResponse(
            text=text,
            abstained=False,
            cited_markers=tuple(sorted({p.marker for p, _, _ in chosen})),
            model_name=self.name,
            model_version=self.version,
            generative=self.generative,
            prompt_characters=len(prompt),
            completion_characters=len(text),
            metadata={
                "quoted_sentences": len(chosen),
                "best_coverage": round(chosen[0][2], 4),
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
    def _quote(passage, sentence: str) -> str:
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
        """
        quoted = f"{sentence.strip()} [{passage.marker}]"

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
