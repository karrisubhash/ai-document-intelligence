"""Retrieval, generation and citation metrics.

Pure functions over plain values, with no Django import, for the same reason
the classification metrics in `ai/` are: these definitions are what every
number this project reports gets measured against, so they should be
readable, exactly specified, and runnable without a database.

Two conventions run through all of them.

*Ranked lists are scored by rank, not by set membership.* Retrieval that
puts the right document first is better than retrieval that puts it tenth,
and a metric that cannot tell those apart is not measuring retrieval.

*An abstention is not a wrong answer.* Scoring "I couldn't find enough
information" as incorrect would punish the exact behaviour section 12.4
requires, so abstentions are counted separately and reported alongside
coverage rather than folded into an accuracy number.
"""

import math
import re
from dataclasses import dataclass, field

WORD = re.compile(r"\w+", re.UNICODE)


# --- Retrieval (spec §19: Recall@K, Precision@K, MRR) -----------------------


def recall_at_k(retrieved, relevant, k: int) -> float:
    """Share of the relevant items that appear in the top k.

    Undefined with no relevant items - returns 0.0 rather than 1.0, because
    "found everything there was to find" is a vacuous claim when there was
    nothing, and reporting it as a perfect score would inflate every
    average it lands in.
    """
    relevant = set(relevant)
    if not relevant or k <= 0:
        return 0.0

    top = set(retrieved[:k])
    return len(top & relevant) / len(relevant)


def precision_at_k(retrieved, relevant, k: int) -> float:
    """Share of the top k that is relevant.

    The denominator is how many were actually returned, not k: charging a
    system for the four slots it did not fill when only one document exists
    measures the corpus, not the retriever.
    """
    if k <= 0:
        return 0.0

    top = retrieved[:k]
    if not top:
        return 0.0

    relevant = set(relevant)
    return sum(1 for item in top if item in relevant) / len(top)


def reciprocal_rank(retrieved, relevant) -> float:
    """1/rank of the first relevant item, or 0.0 if none is retrieved."""
    relevant = set(relevant)
    for position, item in enumerate(retrieved, start=1):
        if item in relevant:
            return 1.0 / position
    return 0.0


def average_precision(retrieved, relevant) -> float:
    """Mean of the precisions at each rank where a relevant item appears.

    Rewards putting *all* the relevant items high, not just the first -
    which is what reciprocal rank alone cannot see.
    """
    relevant = set(relevant)
    if not relevant:
        return 0.0

    hits = 0
    total = 0.0
    for position, item in enumerate(retrieved, start=1):
        if item in relevant:
            hits += 1
            total += hits / position

    return total / len(relevant)


def ndcg_at_k(retrieved, relevant, k: int) -> float:
    """Normalised discounted cumulative gain, binary relevance.

    Included because it discounts smoothly by rank rather than caring only
    about the first hit, which matters when several passages are relevant.
    """
    relevant = set(relevant)
    if not relevant or k <= 0:
        return 0.0

    gain = sum(
        1.0 / math.log2(position + 1)
        for position, item in enumerate(retrieved[:k], start=1)
        if item in relevant
    )
    ideal = sum(
        1.0 / math.log2(position + 1)
        for position in range(1, min(len(relevant), k) + 1)
    )
    return gain / ideal if ideal else 0.0


# --- Citations (spec §19: citation accuracy and source coverage) ------------


def citation_accuracy(cited, relevant) -> float:
    """Share of the cited sources that are actually relevant.

    This is the metric that catches a confident answer citing the wrong
    document. An answer citing nothing scores 0.0, not 1.0: no citations is
    a failure to ground, not a perfect record of never being wrong.
    """
    cited = list(cited)
    if not cited:
        return 0.0

    relevant = set(relevant)
    return sum(1 for item in cited if item in relevant) / len(cited)


def source_coverage(cited, relevant) -> float:
    """Share of the relevant sources the answer actually cited."""
    relevant = set(relevant)
    if not relevant:
        return 0.0
    return len(set(cited) & relevant) / len(relevant)


# --- Generation -------------------------------------------------------------


def token_overlap(answer: str, reference: str) -> float:
    """Bag-of-words overlap with a reference answer.

    Deliberately named for what it measures. It is NOT answer correctness:
    a fluent paraphrase scores badly and a wrong answer built from the right
    vocabulary scores well. It is reported as a weak signal alongside
    groundedness, never as a correctness claim - judging correctness needs
    either a human or a model, and this project does not pretend a word
    count is either.
    """
    answer_terms = set(WORD.findall(answer.lower()))
    reference_terms = set(WORD.findall(reference.lower()))

    if not reference_terms:
        return 0.0

    return len(answer_terms & reference_terms) / len(reference_terms)


def abstention_correct(abstained: bool, expected_abstention: bool) -> bool:
    """Whether abstaining (or not) was the right call for this case.

    Needs negative cases in the dataset to mean anything: a benchmark of
    only answerable questions cannot tell a well-calibrated system from one
    that never abstains.
    """
    return abstained == expected_abstention


# --- Aggregation ------------------------------------------------------------


@dataclass
class RetrievalReport:
    recall_at_k: dict = field(default_factory=dict)
    precision_at_k: dict = field(default_factory=dict)
    mrr: float = 0.0
    map: float = 0.0
    ndcg_at_k: dict = field(default_factory=dict)
    cases: int = 0

    def to_dict(self) -> dict:
        return {
            "recall_at_k": {str(k): round(v, 4) for k, v in self.recall_at_k.items()},
            "precision_at_k": {
                str(k): round(v, 4) for k, v in self.precision_at_k.items()
            },
            "mrr": round(self.mrr, 4),
            "map": round(self.map, 4),
            "ndcg_at_k": {str(k): round(v, 4) for k, v in self.ndcg_at_k.items()},
            "cases": self.cases,
        }


@dataclass
class GenerationReport:
    groundedness: float = 0.0
    citation_accuracy: float = 0.0
    source_coverage: float = 0.0
    token_overlap: float = 0.0
    abstention_accuracy: float = 0.0
    # Split out rather than averaged together: a system that abstains on
    # everything and one that answers everything wrongly can otherwise
    # produce the same headline number.
    answered: int = 0
    abstained: int = 0
    correct_abstentions: int = 0
    false_abstentions: int = 0
    missed_abstentions: int = 0
    cases: int = 0

    @property
    def coverage(self) -> float:
        return self.answered / self.cases if self.cases else 0.0

    def to_dict(self) -> dict:
        return {
            "groundedness": round(self.groundedness, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "source_coverage": round(self.source_coverage, 4),
            "token_overlap": round(self.token_overlap, 4),
            "abstention_accuracy": round(self.abstention_accuracy, 4),
            "coverage": round(self.coverage, 4),
            "answered": self.answered,
            "abstained": self.abstained,
            "correct_abstentions": self.correct_abstentions,
            # A question the corpus answers, refused: the system was too shy.
            "false_abstentions": self.false_abstentions,
            # A question the corpus cannot answer, answered anyway: the
            # failure mode section 12.4 exists to prevent.
            "missed_abstentions": self.missed_abstentions,
            "cases": self.cases,
        }


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def aggregate_retrieval(per_case, ks=(1, 3, 5, 10)) -> RetrievalReport:
    """`per_case` is an iterable of (retrieved_ids, relevant_ids)."""
    cases = [(list(r), set(rel)) for r, rel in per_case]
    scored = [(r, rel) for r, rel in cases if rel]

    report = RetrievalReport(cases=len(scored))
    if not scored:
        return report

    for k in ks:
        report.recall_at_k[k] = _mean(recall_at_k(r, rel, k) for r, rel in scored)
        report.precision_at_k[k] = _mean(
            precision_at_k(r, rel, k) for r, rel in scored
        )
        report.ndcg_at_k[k] = _mean(ndcg_at_k(r, rel, k) for r, rel in scored)

    report.mrr = _mean(reciprocal_rank(r, rel) for r, rel in scored)
    report.map = _mean(average_precision(r, rel) for r, rel in scored)
    return report


def aggregate_generation(per_case) -> GenerationReport:
    """`per_case` is an iterable of dicts, one per evaluated question.

    Expected keys: abstained, expected_abstention, groundedness,
    cited_documents, relevant_documents, answer, expected_answer.
    """
    cases = list(per_case)
    report = GenerationReport(cases=len(cases))
    if not cases:
        return report

    answered = [c for c in cases if not c["abstained"]]
    report.answered = len(answered)
    report.abstained = len(cases) - len(answered)

    for case in cases:
        if case["abstained"] and case["expected_abstention"]:
            report.correct_abstentions += 1
        elif case["abstained"] and not case["expected_abstention"]:
            report.false_abstentions += 1
        elif not case["abstained"] and case["expected_abstention"]:
            report.missed_abstentions += 1

    report.abstention_accuracy = _mean(
        abstention_correct(c["abstained"], c["expected_abstention"]) for c in cases
    )

    # Answer-quality metrics are averaged over ANSWERED cases only. Folding
    # abstentions in at 0.0 would make a cautious system look inaccurate
    # rather than cautious, which is the opposite of what this project wants
    # to encourage.
    if answered:
        report.groundedness = _mean(c.get("groundedness", 0.0) for c in answered)
        report.citation_accuracy = _mean(
            citation_accuracy(c.get("cited_documents", []), c.get("relevant_documents", []))
            for c in answered
        )
        report.source_coverage = _mean(
            source_coverage(c.get("cited_documents", []), c.get("relevant_documents", []))
            for c in answered
        )
        report.token_overlap = _mean(
            token_overlap(c.get("answer", ""), c.get("expected_answer", ""))
            for c in answered
            if c.get("expected_answer")
        )

    return report


# Metrics where a SMALLER number is the better outcome. Without this, a
# comparison treats every increase as an improvement - so a run that got
# slower, or that started refusing questions it used to answer, would be
# reported as a win. That is the exact failure a regression check exists to
# catch, so getting the direction wrong makes the check worse than absent.
LOWER_IS_BETTER = (
    "latency",
    "duration",
    "false_abstentions",
    "missed_abstentions",
)

# Metrics that describe the shape of a run rather than its quality. A run
# over more cases is not "better", so these are reported as changed without
# being called a regression or an improvement either way.
NEUTRAL = ("cases", "answered", "abstained", "correct_abstentions")


def metric_direction(key: str) -> str:
    """"higher", "lower" or "neutral" for a flattened metric key."""
    leaf = key.rsplit(".", 1)[-1]

    if any(token in leaf for token in LOWER_IS_BETTER):
        return "lower"
    if leaf in NEUTRAL:
        return "neutral"
    return "higher"


def compare(baseline: dict, candidate: dict, *, tolerance: float = 0.0) -> dict:
    """Flat deltas between two runs' metric dicts, for regression checks.

    Only keys present in BOTH are compared. A metric that exists in one run
    and not the other is reported as added or removed rather than silently
    treated as a change from zero, which would read as a catastrophic
    regression every time a new metric is introduced.

    Direction is per metric - see LOWER_IS_BETTER.
    """
    flat_baseline = _flatten(baseline)
    flat_candidate = _flatten(candidate)

    shared = sorted(set(flat_baseline) & set(flat_candidate))
    deltas = {}
    directions = {}
    regressions = []
    improvements = []
    changed = []

    for key in shared:
        before, after = flat_baseline[key], flat_candidate[key]
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue
        if isinstance(before, bool) or isinstance(after, bool):
            continue

        delta = after - before
        deltas[key] = round(delta, 6)
        direction = metric_direction(key)
        directions[key] = direction

        if abs(delta) <= tolerance:
            continue

        if direction == "neutral":
            changed.append(key)
        elif (direction == "higher") == (delta > 0):
            improvements.append(key)
        else:
            regressions.append(key)

    return {
        "deltas": deltas,
        "directions": directions,
        "regressions": regressions,
        "improvements": improvements,
        # Moved, but not in a direction that means better or worse.
        "changed": changed,
        "added": sorted(set(flat_candidate) - set(flat_baseline)),
        "removed": sorted(set(flat_baseline) - set(flat_candidate)),
    }


def _flatten(data: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat
