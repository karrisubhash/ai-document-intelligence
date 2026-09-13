"""Reranking: reordering retrieved candidates by query-document relevance.

Retrieval optimises for recall - it casts a wide net cheaply. Reranking
optimises for precision over that smaller set, and it is what decides which
passages actually reach the context window. Getting this wrong is expensive
in a way retrieval is not: a bad candidate at rank 40 is harmless, a bad
passage at rank 1 is what the answer gets built from.

The bundled reranker is a deterministic lexical scorer, for the same reason
the bundled embedder is: no model is wired up in this project, and shipping
the mechanism with an honest baseline beats shipping neither. It reports
`semantic = False` so a caller can tell what kind of relevance it measured.
A cross-encoder is a settings change.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

TERM = re.compile(r"\w+", re.UNICODE)

# Words too common to signal relevance, plus the interrogatives and
# auxiliaries that carry a question's grammar rather than its subject.
# Counting those in the denominator punishes a passage for not repeating
# "how many can a" - which is how "How many books can a student borrow?"
# scored 2/6 against a passage that answers it exactly.
#
# Deliberately excludes "no" and "not": they matter in institutional text
# ("circular no", "shall not be permitted").
STOPWORDS = frozenset(
    """a an the of to in for on at by with and or is are was were be been
    this that these those it its as from
    what when where which who whom whose why how
    can could may might shall should will would must do does did
    i you he she we they me him her us them my your our their
    any some each much many more most other another
    please tell give show explain about
    """.split()
)

# Suffixes stripped for matching only. This is not linguistics - it is the
# minimum needed so "borrow" matches "borrowing" and "book" matches "books",
# which is the difference between answering a question and abstaining on it.
# Order matters: longest first.
SUFFIXES = ("ements", "ement", "ingly", "ings", "ing", "ies", "ied", "ed", "es", "s")

# Below this a stem is left alone. Stripping "s" from "gas" or "ing" from
# "ring" would merge unrelated words.
MIN_STEM = 4


def stem(term: str) -> str:
    """Crude suffix stripping, applied to both sides of every comparison."""
    if term.isdigit() or len(term) <= MIN_STEM:
        return term

    for suffix in SUFFIXES:
        if term.endswith(suffix) and len(term) - len(suffix) >= MIN_STEM:
            root = term[: -len(suffix)]
            # "ies" -> "y" keeps policies/policy together.
            if suffix == "ies":
                return root + "y"
            return root

    return term


@dataclass
class RerankedChunk:
    chunk: object
    score: float
    retrieval_score: float = 0.0
    signals: dict = field(default_factory=dict)


@runtime_checkable
class Reranker(Protocol):
    """Reorders candidates for one query. Never adds or invents candidates."""

    name: str
    version: str
    semantic: bool

    def rerank(self, query: str, candidates: list) -> list: ...


def _terms(text: str) -> list:
    """Content terms, stemmed. The single definition used by reranking, the
    extractive answerer and grounding, so all three agree on what a term is."""
    return [
        stem(term)
        for term in TERM.findall(text.lower())
        if len(term) > 1 and term not in STOPWORDS
    ]


class LexicalReranker:
    """Scores overlap between query terms and a passage.

    Three signals, each answering a different question about a passage:
    coverage (how much of the query it addresses), density (whether it is
    about the query or merely mentions it), and proximity (whether the query
    terms appear together rather than scattered).
    """

    name = "lexical-reranker"
    version = "0.1.0"
    semantic = False

    # A passage covering more of the query is more likely to answer it, so
    # coverage carries the most weight.
    COVERAGE_WEIGHT = 0.6
    DENSITY_WEIGHT = 0.2
    PROXIMITY_WEIGHT = 0.2

    def rerank(self, query: str, candidates: list) -> list:
        query_terms = _terms(query)
        if not query_terms:
            return [
                RerankedChunk(chunk=c.chunk, score=0.0, retrieval_score=c.score)
                for c in candidates
            ]

        unique_query = set(query_terms)
        scored = []

        for candidate in candidates:
            text = candidate.chunk.text
            passage_terms = _terms(text)
            if not passage_terms:
                scored.append(
                    RerankedChunk(
                        chunk=candidate.chunk,
                        score=0.0,
                        retrieval_score=candidate.score,
                        signals={"reason": "passage has no scoreable terms"},
                    )
                )
                continue

            positions = {}
            for index, term in enumerate(passage_terms):
                if term in unique_query:
                    positions.setdefault(term, []).append(index)

            matched = set(positions)
            coverage = len(matched) / len(unique_query)

            hits = sum(len(v) for v in positions.values())
            # Log-damped: the tenth occurrence of a term says much less than
            # the second, and without damping a long passage that repeats
            # one query word outranks a short one that answers the question.
            density = math.log1p(hits) / math.log1p(len(passage_terms))

            proximity = self._proximity(positions, len(passage_terms))

            score = (
                self.COVERAGE_WEIGHT * coverage
                + self.DENSITY_WEIGHT * density
                + self.PROXIMITY_WEIGHT * proximity
            )

            scored.append(
                RerankedChunk(
                    chunk=candidate.chunk,
                    score=round(score, 6),
                    retrieval_score=candidate.score,
                    signals={
                        "coverage": round(coverage, 4),
                        "density": round(density, 4),
                        "proximity": round(proximity, 4),
                        "matched_terms": sorted(matched),
                        "query_terms": len(unique_query),
                    },
                )
            )

        # Ties broken by retrieval score, then chunk id, so the order is
        # deterministic rather than dependent on dict iteration.
        scored.sort(key=lambda r: (-r.score, -r.retrieval_score, r.chunk.id))
        return scored

    @staticmethod
    def _proximity(positions: dict, length: int) -> float:
        """1.0 when every matched term sits together, decaying as they spread."""
        flat = sorted(index for indexes in positions.values() for index in indexes)
        if len(flat) < 2:
            return 1.0 if flat else 0.0

        span = flat[-1] - flat[0] + 1
        ideal = len(flat)
        return min(1.0, ideal / span)
