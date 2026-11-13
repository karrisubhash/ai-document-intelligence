"""Extractive summary, key points and key phrases - chosen, never written.

THE SUMMARY is 3-5 of the document's own sentences. Each candidate is scored
by how central it is (cosine of its TF-IDF vector to the whole document's,
optionally blended with an embedding similarity when the configured model is
semantic), plus a prior for appearing early - institutional documents state
their point first - and the final set is picked with maximal marginal
relevance so the third sentence is not a paraphrase of the first. Sentences
are returned in reading order, each with its span.

KEY POINTS are the sentences a reader acts on: obligations ("must",
"shall"), deadlines, figures, dates and numbered clauses, with headings as a
last resort - each tagged with the `kind` that qualified it, at most two of
a kind, never a sentence the summary already shows.

KEY PHRASES are 1-4 word runs between stopwords and punctuation, scored by
frequency in this document times inverse document frequency across the
corpus (when the corpus is big enough for that to mean anything), so
"examination fee" outranks "institute" in a corpus where every document
mentions the institute.
"""

import math
import re
from collections import Counter

from .text import STOPWORDS, collapse, content_terms, normalize_term

SUMMARY_MIN = 3
SUMMARY_MAX = 5
KEY_POINTS_MAX = 6
KEY_PHRASES_MAX = 10
MMR_LAMBDA = 0.7
# Candidate sentences are bounded so a 500-page regulation costs what a
# 50-page one does. Early sentences are preferred anyway (position prior).
MAX_CANDIDATES = 1_500

OBLIGATION = re.compile(
    r"\b(?:must|shall|required\s+to|is\s+required|are\s+required|mandatory|"
    r"compulsory|not\s+permitted|prohibited|is\s+not\s+allowed|will\s+not\s+be|"
    r"should)\b",
    re.IGNORECASE,
)
CLAUSE_START = re.compile(
    r"^\s*(?:(?:clause|section|rule|article)\s+[A-Z]?\d+(?:\.\d+)*|\d{1,2}(?:\.\d{1,2})+|"
    r"\(?[a-h]\)|\(?[ivx]{1,4}\))",
    re.IGNORECASE,
)
NUMBER = re.compile(r"\d")
# Phrases never span punctuation, digits, line breaks or a spaced dash. A
# dot between letters ("B.Tech") is part of a word, not a break.
PHRASE_BREAK = re.compile(r"[^\w\s'&.-]|(?<![^\W\d_])\.|\.(?![^\W\d_])|\d|\n|\s-\s|\t")
PHRASE_WORD = re.compile(r"[^\W\d_](?:[\w'’-]|\.(?=[^\W\d_]))*", re.UNICODE)
# Words that structure or connect institutional text without saying what
# it is about. Corpus IDF demotes most of them on a large corpus; this list
# keeps them out of a small one too.
PHRASE_STOPWORDS = STOPWORDS | frozenset(
    """
    clause clauses section sections rule rules unit units chapter part page
    annexure appendix item items point note notes following given above below
    last late first second third new same total various certain whole hereby
    therein thereof herein said apply applies applied applicable issued issue
    leaves leave made make makes taken take takes accepted based include
    includes including required require requires respect regard regarding
    case cases time times day days year years month months week weeks
    """.split()
)
KIND_WEIGHT = {
    "deadline": 1.0,
    "obligation": 0.9,
    "figure": 0.8,
    "date": 0.7,
    "clause": 0.6,
    "heading": 0.3,
}


def _letter_ratio(text):
    visible = [c for c in text if not c.isspace()]
    if not visible:
        return 0.0
    return sum(1 for c in visible if c.isalpha()) / len(visible)


def is_prose(segment) -> bool:
    """A sentence a reader could be shown on its own."""
    if segment.is_heading:
        return False
    text = segment.text
    words = segment.words
    if not 6 <= len(words) <= 70 or not 30 <= len(text) <= 450:
        return False
    if "|" in text or "\t" in text or "   " in text:
        return False
    return _letter_ratio(text) >= 0.6


def _overlaps(segment, regions):
    return any(
        page == segment.page_number and segment.start < end and start < segment.end
        for page, start, end in regions
    )


def _vector(terms, idf):
    counts = Counter(terms)
    vector = {term: (1 + math.log(count)) * idf(term) for term, count in counts.items()}
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm == 0:
        return {}
    return {term: value / norm for term, value in vector.items()}


def _cosine(left, right):
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())


class Scorer:
    """TF-IDF over the document's sentences, optionally corpus-weighted."""

    def __init__(self, segments, corpus_idf=None):
        self.corpus_idf = corpus_idf
        self.terms = {s.position: content_terms(s.text) for s in segments}
        frequency = Counter()
        for terms in self.terms.values():
            frequency.update(set(terms))
        self.local_df = frequency
        self.n = max(1, len(self.terms))
        self.vectors = {
            position: _vector(terms, self.idf) for position, terms in self.terms.items()
        }
        centroid = Counter()
        for vector in self.vectors.values():
            centroid.update(vector)
        norm = math.sqrt(sum(v * v for v in centroid.values())) or 1.0
        self.centroid = {term: value / norm for term, value in centroid.items()}

    def idf(self, term):
        local = math.log((self.n + 1) / (self.local_df.get(term, 0) + 1)) + 1.0
        if self.corpus_idf is None:
            return local
        # Within-document rarity says what a sentence is specifically about;
        # corpus rarity says what makes this document unlike the others.
        return math.sqrt(local * self.corpus_idf(term))

    def centrality(self, segment):
        return _cosine(self.vectors.get(segment.position, {}), self.centroid)

    def similarity(self, left, right):
        return _cosine(
            self.vectors.get(left.position, {}), self.vectors.get(right.position, {})
        )


def summarize(segments, *, excluded_regions=(), corpus_idf=None, semantic=None):
    """The summary sentences, in reading order, each with its score.

    `semantic(candidates)` may return a list of similarities (to the
    document's embedding centroid) for the given candidates, or None.
    """
    candidates = [
        s for s in segments if is_prose(s) and not _overlaps(s, excluded_regions)
    ][:MAX_CANDIDATES]
    if not candidates:
        return [], None

    scorer = Scorer(candidates, corpus_idf=corpus_idf)
    total = len(candidates)
    scores = {}
    for index, segment in enumerate(candidates):
        prior = 0.3 + 0.7 * math.exp(-3.0 * index / max(total, 1))
        scores[segment.position] = 0.7 * scorer.centrality(segment) + 0.3 * prior

    used_semantic = False
    if semantic is not None:
        shortlist = sorted(candidates, key=lambda s: -scores[s.position])[:40]
        similarities = semantic(shortlist)
        if similarities:
            used_semantic = True
            for segment, similarity in zip(shortlist, similarities, strict=True):
                scores[segment.position] = 0.6 * scores[segment.position] + 0.4 * max(
                    similarity, 0.0
                )
            shortlisted = {s.position for s in shortlist}
            for segment in candidates:
                if segment.position not in shortlisted:
                    scores[segment.position] *= 0.6

    target = SUMMARY_MIN if total < 15 else 4 if total < 60 else SUMMARY_MAX
    chosen = _mmr(candidates, scores, scorer, target)
    chosen.sort(key=lambda s: s.position)
    return [(segment, round(scores[segment.position], 4)) for segment in chosen], {
        "candidates": total,
        "semantic": used_semantic,
        "scorer": scorer,
    }


def _mmr(candidates, scores, scorer, target):
    chosen = []
    pool = sorted(candidates, key=lambda s: -scores[s.position])[:200]
    while pool and len(chosen) < target:
        best, best_value = None, None
        for segment in pool:
            redundancy = max(
                (scorer.similarity(segment, picked) for picked in chosen), default=0.0
            )
            value = (
                MMR_LAMBDA * scores[segment.position] - (1 - MMR_LAMBDA) * redundancy
            )
            if best_value is None or value > best_value:
                best, best_value = segment, value
        # A near-duplicate of a chosen sentence is not worth showing.
        if chosen and max(scorer.similarity(best, picked) for picked in chosen) > 0.8:
            pool.remove(best)
            continue
        chosen.append(best)
        pool.remove(best)
    return chosen


def key_points(
    segments, entities, *, summary_positions=(), excluded_regions=(), scorer=None
):
    """Up to KEY_POINTS_MAX sentences a reader acts on, each with its kind."""
    by_page = {}
    for entity in entities:
        by_page.setdefault(entity.page_number, []).append(entity)

    def kinds_of(segment):
        inside = [
            e
            for e in by_page.get(segment.page_number, ())
            if segment.start <= e.start and e.end <= segment.end
        ]
        types = {e.entity_type for e in inside}
        kinds = []
        if "DEADLINE" in types:
            kinds.append("deadline")
        if OBLIGATION.search(segment.text):
            kinds.append("obligation")
        if types & {"MONEY", "PERCENT", "DURATION"}:
            kinds.append("figure")
        if "DATE" in types:
            kinds.append("date")
        if CLAUSE_START.match(segment.text):
            kinds.append("clause")
        return kinds

    scored = []
    for segment in segments:
        if segment.position in summary_positions or _overlaps(
            segment, excluded_regions
        ):
            continue
        if segment.is_heading:
            continue
        words = segment.words
        if not 5 <= len(words) <= 70 or len(segment.text) > 450:
            continue
        if "|" in segment.text or "\t" in segment.text:
            continue
        kinds = kinds_of(segment)
        if not kinds:
            continue
        centrality = scorer.centrality(segment) if scorer else 0.0
        weight = KIND_WEIGHT[kinds[0]] + 0.1 * (len(kinds) - 1) + 0.5 * centrality
        scored.append((weight, segment, kinds[0]))

    scored.sort(key=lambda item: (-item[0], item[1].position))
    picked, per_kind, seen = [], Counter(), []
    for _, segment, kind in scored:
        if len(picked) >= KEY_POINTS_MAX:
            break
        if per_kind[kind] >= 2:
            continue
        normalized = collapse(segment.text).lower()
        if any(normalized == other for other in seen):
            continue
        picked.append((segment, kind))
        per_kind[kind] += 1
        seen.append(normalized)

    if len(picked) < KEY_POINTS_MAX:
        # Section headings, as a last resort and at most two: an outline
        # entry says where to look, not what was decided. The opening
        # lines of page one are the title block, not sections.
        first_page = segments[0].page_number if segments else None
        title_block = {s.position for s in segments[:3] if s.page_number == first_page}
        headings = [
            s
            for s in segments
            if s.is_heading
            and s.position not in title_block
            and 1 <= len(s.words) <= 10
        ]
        for segment in headings[: min(2, KEY_POINTS_MAX - len(picked))]:
            picked.append((segment, "heading"))

    picked.sort(key=lambda item: item[0].position)
    return picked


def _phrases(text):
    """Candidate phrases: runs of 1-4 content words, with their context.

    Yields (words, acronyms): `acronyms` says which of the words are written
    in capitals inside a line that is not itself all capitals - "IT", "CGPA" -
    as opposed to a word in a shouted heading.
    """
    for line in text.splitlines():
        shouted = not any(c.islower() for c in line)
        for chunk in PHRASE_BREAK.split(line):
            words = PHRASE_WORD.findall(chunk)
            run = []
            for word in [*words, None]:
                if word is None or word.lower() in PHRASE_STOPWORDS or len(word) < 3:
                    if run:
                        for size in range(1, min(4, len(run)) + 1):
                            for start in range(0, len(run) - size + 1):
                                yield run[start : start + size], shouted
                    run = []
                    continue
                run.append(word)


def _display_word(word, casing):
    if "." in word:
        return word
    return word if casing.get(word.lower()) else word.lower()


def key_phrases(pages, *, corpus_idf=None):
    """The top phrases, best first, as (display text, normalised score)."""
    counts = Counter()
    forms = {}
    # Per word: True while every occurrence is an acronym in mixed text.
    casing = {}
    for _, text in pages:
        for words, shouted in _phrases(text or ""):
            for word in words:
                acronym = word.isupper() and len(word) <= 5 and not shouted
                lowered = word.lower()
                casing[lowered] = casing.get(lowered, True) and acronym
            key = tuple(normalize_term(word) for word in words)
            counts[key] += 1
            forms.setdefault(key, words)

    display = {
        key: " ".join(_display_word(word, casing) for word in words)
        for key, words in forms.items()
    }

    if not counts:
        return []

    def idf(term):
        return corpus_idf(term) if corpus_idf is not None else 1.0

    scored = {}
    for key, count in counts.items():
        if count < 2 and len(counts) > 30:
            continue
        mean_idf = sum(idf(term) for term in key) / len(key)
        # Longer phrases are rarer by construction; a bonus keeps
        # "examination fee" above "fee" without letting every 4-word run win.
        scored[key] = (1 + math.log(count)) * mean_idf * (1 + 0.5 * (len(key) - 1))

    ranked = sorted(scored.items(), key=lambda item: (-item[1], display[item[0]]))
    chosen = []
    for key, score in ranked:
        if len(chosen) >= KEY_PHRASES_MAX:
            break
        # Not a word of a phrase already chosen, nor a phrase containing one
        # that scored higher.
        if any(_contains(other, key) or _contains(key, other) for other, _ in chosen):
            continue
        chosen.append((key, score))

    top = chosen[0][1] if chosen else 1.0
    return [(display[key], round(score / top, 4)) for key, score in chosen]


def _contains(outer, inner):
    if len(inner) > len(outer):
        return False
    return any(
        outer[start : start + len(inner)] == inner
        for start in range(len(outer) - len(inner) + 1)
    )
