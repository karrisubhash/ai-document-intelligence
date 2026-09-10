"""Classification metrics, computed in pure Python.

No numpy/sklearn dependency on purpose: these definitions are the thing
every result in this project gets reported against, so they should be
readable, exactly specified, and runnable anywhere - including in a
Kaggle kernel before the ML stack is imported, or here with no install
at all.

Macro averages weight every class equally, which is the point for a
16-class problem where the interesting failures are in the small,
low-text classes rather than in the aggregate.
"""

from dataclasses import dataclass, field


@dataclass
class ClassMetrics:
    label: str
    support: int
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "support": self.support,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class EvaluationReport:
    labels: list
    per_class: list
    confusion: list
    accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    weighted_f1: float
    total: int
    abstained: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "accuracy": round(self.accuracy, 4),
            "macro_precision": round(self.macro_precision, 4),
            "macro_recall": round(self.macro_recall, 4),
            "macro_f1": round(self.macro_f1, 4),
            "weighted_f1": round(self.weighted_f1, 4),
            "total": self.total,
            "abstained": self.abstained,
            "labels": list(self.labels),
            "per_class": [c.to_dict() for c in self.per_class],
            "confusion_matrix": self.confusion,
            **self.extra,
        }

    def most_confused(self, limit: int = 10) -> list:
        """Off-diagonal pairs, largest first.

        This is where a 16-class document problem actually goes wrong -
        letter/memo and form/questionnaire rather than the headline number.
        """
        pairs = []
        for i, actual in enumerate(self.labels):
            for j, predicted in enumerate(self.labels):
                if i != j and self.confusion[i][j]:
                    pairs.append(
                        {
                            "actual": actual,
                            "predicted": predicted,
                            "count": self.confusion[i][j],
                            "share_of_actual": round(
                                self.confusion[i][j] / max(1, sum(self.confusion[i])), 4
                            ),
                        }
                    )
        return sorted(pairs, key=lambda p: -p["count"])[:limit]


def evaluate(
    y_true: list,
    y_pred: list,
    labels: list,
    *,
    abstain_label: str | None = None,
) -> EvaluationReport:
    """Score predictions against truth.

    Abstentions (predictions equal to `abstain_label`) are counted and
    excluded from the confusion matrix and per-class metrics, because
    scoring "I don't know" as a wrong answer hides whether abstention is
    working. Coverage is reported alongside so the two cannot be read
    apart.
    """
    if len(y_true) != len(y_pred):
        raise ValueError(
            f"y_true has {len(y_true)} items but y_pred has {len(y_pred)}"
        )

    index = {label: i for i, label in enumerate(labels)}
    unknown = {label for label in y_true if label not in index}
    if unknown:
        raise ValueError(f"y_true contains labels outside `labels`: {sorted(unknown)}")

    size = len(labels)
    confusion = [[0] * size for _ in range(size)]
    abstained = 0

    for actual, predicted in zip(y_true, y_pred):
        if abstain_label is not None and predicted == abstain_label:
            abstained += 1
            continue
        if predicted not in index:
            raise ValueError(f"prediction {predicted!r} is not a known label")
        confusion[index[actual]][index[predicted]] += 1

    per_class = []
    for i, label in enumerate(labels):
        true_positives = confusion[i][i]
        false_negatives = sum(confusion[i]) - true_positives
        false_positives = sum(confusion[r][i] for r in range(size)) - true_positives
        per_class.append(
            ClassMetrics(
                label=label,
                support=sum(confusion[i]),
                true_positives=true_positives,
                false_positives=false_positives,
                false_negatives=false_negatives,
            )
        )

    scored = sum(sum(row) for row in confusion)
    correct = sum(confusion[i][i] for i in range(size))
    supports = sum(c.support for c in per_class)

    return EvaluationReport(
        labels=list(labels),
        per_class=per_class,
        confusion=confusion,
        accuracy=correct / scored if scored else 0.0,
        macro_precision=sum(c.precision for c in per_class) / size if size else 0.0,
        macro_recall=sum(c.recall for c in per_class) / size if size else 0.0,
        macro_f1=sum(c.f1 for c in per_class) / size if size else 0.0,
        weighted_f1=(
            sum(c.f1 * c.support for c in per_class) / supports if supports else 0.0
        ),
        total=len(y_true),
        abstained=abstained,
        extra={"coverage": round(scored / len(y_true), 4) if y_true else 0.0},
    )


def render(report: EvaluationReport) -> str:
    lines = [
        f"accuracy      {report.accuracy:.4f}",
        f"macro-F1      {report.macro_f1:.4f}"
        f"   (P {report.macro_precision:.4f} / R {report.macro_recall:.4f})",
        f"weighted-F1   {report.weighted_f1:.4f}",
        f"scored        {report.total - report.abstained}/{report.total}"
        f"  (abstained {report.abstained})",
        "",
        f"{'class':<24}{'support':>9}{'prec':>8}{'recall':>8}{'f1':>8}",
        "-" * 57,
    ]

    for metrics in sorted(report.per_class, key=lambda c: c.f1):
        lines.append(
            f"{metrics.label:<24}{metrics.support:>9}"
            f"{metrics.precision:>8.3f}{metrics.recall:>8.3f}{metrics.f1:>8.3f}"
        )

    confused = report.most_confused(8)
    if confused:
        lines += ["", "most confused pairs:"]
        for pair in confused:
            lines.append(
                f"  {pair['actual']:<24} -> {pair['predicted']:<24}"
                f"{pair['count']:>6}  ({pair['share_of_actual']:.1%} of actual)"
            )

    return "\n".join(lines)


def render_confusion(report: EvaluationReport, width: int = 5) -> str:
    """Text confusion matrix - always available, even where matplotlib
    is not. Rows are actual, columns predicted."""
    header = " " * 26 + "".join(f"{i:>{width}}" for i in range(len(report.labels)))
    lines = [header, " " * 26 + "-" * (width * len(report.labels))]

    for i, label in enumerate(report.labels):
        row = "".join(f"{value:>{width}}" for value in report.confusion[i])
        lines.append(f"{i:>2} {label:<22}{row}")

    return "\n".join(lines)
