"""Train the TF-IDF + linear text baseline on QS-OCR manifests.

Runs to completion on a machine with no GPU, which is why this is the
first baseline rather than the second. Everything it needs was fixed
before it ran: the splits come from committed manifests that the
integrity gate has already checked for leakage, and the hyperparameters
come from configs/text_baseline.yaml rather than from this file.

    manifests (train/validation/test)
        |
        v
    normalize -> TF-IDF -> logistic regression
        |
        +-- abstention threshold fitted on VALIDATION
        |
        v
    evaluated on TEST, once
        |
        v
    artifacts/<model>/v<version>/{model.joblib,metrics,metadata,labels}

The abstention threshold is *fitted*, not guessed, and the whole sweep
it was chosen from is written into metrics.json so the choice can be
audited instead of taken on trust.

The artifact contains only scikit-learn and builtin objects - no class
defined in this package. That is deliberate: the Django inference
adapter must be able to load it without importing `ai`, which is the
boundary the whole project is built around.

Usage:
    export PYTHONPATH=.
    python -m ai.classification.training.train_text
    python -m ai.classification.training.train_text --smoke
"""

import argparse
import json
import time
from pathlib import Path

import joblib
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from ai.classification.datasets.base import LABELS, read_manifest
from ai.classification.evaluation.metrics import evaluate, render, render_confusion
from ai.classification.preprocessing import text as text_preprocessing

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "ai" / "classification" / "configs" / "text_baseline.yaml"
DEFAULT_ARTIFACTS = REPO_ROOT / "ai" / "classification" / "artifacts"

# Must match documents.processing.classification.base.UNKNOWN_LABEL on the
# Django side. A document the model cannot place is allowed to come back
# unknown rather than be forced into one of the sixteen classes.
UNKNOWN_LABEL = "unknown"

# The official RVL-CDIP train split. Only a run at that scale may be
# compared against published numbers; anything smaller is a different
# experiment and is flagged as such in the artifact metadata.
OFFICIAL_TRAIN_SIZE = 320_000

SPLITS = ("train", "validation", "test")


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_split(manifest_dir: Path, split: str, per_class: int | None):
    """Read one manifest, optionally capping samples per class.

    The cap is for smoke runs only. It keeps the classes balanced by
    taking the first N of each in manifest order, which is already a
    deterministic hash ordering from the dataset adapter.
    """
    samples = read_manifest(manifest_dir / f"{split}.csv")

    if per_class is None:
        return samples

    seen: dict[str, int] = {}
    capped = []
    for sample in samples:
        count = seen.get(sample.label, 0)
        if count < per_class:
            capped.append(sample)
            seen[sample.label] = count + 1

    return capped


def build_pipeline(config: dict) -> Pipeline:
    """Assemble the model exactly as the config specifies.

    Note that lowercasing and normalization happen *before* the
    vectorizer sees the text, so TfidfVectorizer's own lowercase step is
    turned off - leaving it on would mean two different components each
    partly responsible for the same transformation.
    """
    features = config["features"]
    classifier = config["classifier"]

    if features["vectorizer"] != "tfidf":
        raise ValueError(f"unsupported vectorizer {features['vectorizer']!r}")
    if classifier["estimator"] != "logistic_regression":
        raise ValueError(f"unsupported estimator {classifier['estimator']!r}")

    return Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    analyzer=features["analyzer"],
                    ngram_range=tuple(features["ngram_range"]),
                    min_df=features["min_df"],
                    max_features=features["max_features"],
                    sublinear_tf=features["sublinear_tf"],
                    lowercase=False,
                ),
            ),
            (
                "classifier",
                LogisticRegression(
                    C=classifier["C"],
                    max_iter=classifier["max_iter"],
                    class_weight=classifier["class_weight"],
                ),
            ),
        ]
    )


def predict_with_threshold(model, texts, threshold: float) -> list[str]:
    """Predict, abstaining below `threshold`.

    The column order of predict_proba is `model.classes_`, which sklearn
    sorts alphabetically - NOT the canonical LABELS order. Reading it off
    the fitted model is the only way to keep probabilities attached to
    the labels they actually belong to.
    """
    classes = list(model.classes_)
    probabilities = model.predict_proba(texts)
    predictions = []

    for row in probabilities:
        best = int(row.argmax())
        predictions.append(
            classes[best] if row[best] >= threshold else UNKNOWN_LABEL
        )

    return predictions


def sweep_thresholds(model, texts, truth, step: float = 0.05) -> list[dict]:
    """Accuracy-when-answering vs coverage, across candidate thresholds.

    Computed once from a single probability matrix rather than by
    re-predicting per threshold - the model is the expensive part and it
    does not change as the cutoff moves.
    """
    classes = list(model.classes_)
    probabilities = model.predict_proba(texts)
    best_index = probabilities.argmax(axis=1)
    best_score = probabilities.max(axis=1)
    predicted = [classes[i] for i in best_index]

    sweep = []
    threshold = 0.0
    while threshold < 1.0:
        answered = correct = 0
        for actual, prediction, score in zip(truth, predicted, best_score):
            if score < threshold:
                continue
            answered += 1
            correct += actual == prediction

        sweep.append(
            {
                "threshold": round(threshold, 2),
                "coverage": round(answered / len(truth), 4) if truth else 0.0,
                "answered": answered,
                # Accuracy among the questions it chose to answer. This is
                # the number abstention is meant to protect.
                "accuracy_when_answering": (
                    round(correct / answered, 4) if answered else 0.0
                ),
            }
        )
        threshold += step

    return sweep


def select_threshold(sweep: list[dict], target_accuracy: float) -> dict:
    """Lowest threshold that reaches the target accuracy when answering.

    Lowest rather than best, because every increment above the one that
    meets the target buys nothing and costs coverage. If no threshold
    reaches the target, the most accurate one is returned with
    target_met false - the shortfall gets reported, never rounded away.
    """
    qualifying = [
        point
        for point in sweep
        if point["answered"] and point["accuracy_when_answering"] >= target_accuracy
    ]

    if qualifying:
        chosen = min(qualifying, key=lambda point: point["threshold"])
        return {**chosen, "target_accuracy": target_accuracy, "target_met": True}

    fallback = max(
        sweep,
        key=lambda point: (point["accuracy_when_answering"], point["coverage"]),
    )
    return {**fallback, "target_accuracy": target_accuracy, "target_met": False}


def measure_latency(model, texts, iterations: int = 200) -> float:
    """Milliseconds per document, predicted one at a time.

    One at a time on purpose: that is how the Django pipeline calls it,
    and batching would report a throughput number that no caller sees.
    """
    sample = texts[:iterations] or texts
    started = time.perf_counter()
    for document in sample:
        model.predict_proba([document])
    elapsed = time.perf_counter() - started

    return round(1000 * elapsed / len(sample), 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest-dir", type=Path, default=None,
                        help="Overrides dataset.manifest_dir from the config.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--model-version", default=None,
                        help="Overrides model_version from the config.")
    parser.add_argument("--per-class", type=int, default=None,
                        help="Cap samples per class (for quick runs).")
    parser.add_argument("--target-accuracy", type=float, default=0.90,
                        help="Accuracy-when-answering the abstention "
                             "threshold is fitted to reach on validation.")
    parser.add_argument("--smoke", action="store_true",
                        help="25 documents/class - proves the path cheaply.")
    args = parser.parse_args()

    config = load_config(args.config)
    per_class = 25 if args.smoke else args.per_class
    model_version = args.model_version or str(config["model_version"])
    model_name = config["model_name"]
    version_label = f"v{model_version}-smoke" if args.smoke else f"v{model_version}"

    manifest_dir = args.manifest_dir or (REPO_ROOT / config["dataset"]["manifest_dir"])
    if not manifest_dir.is_dir():
        raise SystemExit(
            f"No manifests at {manifest_dir}. Build them first:\n"
            f"  python -m ai.classification.datasets.qs_ocr --per-class 500"
        )

    preprocessing = {
        "unicode_normalization": config["preprocessing"]["unicode_normalization"],
        "lowercase": config["preprocessing"]["lowercase"],
        "collapse_whitespace": config["preprocessing"]["collapse_whitespace"],
    }

    print(f"config      {args.config}")
    print(f"manifests   {manifest_dir}")
    print(f"model       {model_name} {version_label} "
          f"({config['architecture']})\n")

    samples = {split: load_split(manifest_dir, split, per_class) for split in SPLITS}
    loading_started = time.perf_counter()
    texts = {
        split: text_preprocessing.load_texts(rows, preprocessing)
        for split, rows in samples.items()
    }
    loading_seconds = time.perf_counter() - loading_started

    truth = {split: [row.label for row in rows] for split, rows in samples.items()}
    empty_counts = {
        split: sum(1 for document in split_texts if not document)
        for split, split_texts in texts.items()
    }

    for split in SPLITS:
        print(f"{split:<11} {len(samples[split]):>6} documents, "
              f"{empty_counts[split]:>4} with no OCR text "
              f"({empty_counts[split] / max(1, len(samples[split])):.2%})")
    print(f"read in {loading_seconds:.1f}s\n")

    model = build_pipeline(config)

    training_started = time.perf_counter()
    model.fit(texts["train"], truth["train"])
    training_seconds = time.perf_counter() - training_started

    vocabulary = len(model.named_steps["tfidf"].vocabulary_)
    print(f"fitted in {training_seconds:.1f}s, vocabulary {vocabulary:,} features\n")

    # --- Abstention threshold: fitted on validation, never on test ------
    sweep = sweep_thresholds(model, texts["validation"], truth["validation"])
    selection = select_threshold(sweep, args.target_accuracy)
    threshold = selection["threshold"]

    print(f"abstention threshold {threshold:.2f} "
          f"(target accuracy-when-answering {args.target_accuracy:.0%}, "
          f"met: {selection['target_met']})")
    print(f"  on validation: answers {selection['coverage']:.1%} of documents "
          f"at {selection['accuracy_when_answering']:.1%} accuracy\n")

    if not selection["target_met"]:
        print("  NOTE: no threshold reached the target. The value above is "
              "the best available, not a passing result.\n")

    # --- Evaluation ----------------------------------------------------
    results = {"preprocessing": preprocessing, "threshold_sweep": sweep,
               "threshold_selection": selection}

    for split in ("validation", "test"):
        raw = evaluate(truth[split], model.predict(texts[split]), list(LABELS))
        gated = evaluate(
            truth[split],
            predict_with_threshold(model, texts[split], threshold),
            list(LABELS),
            abstain_label=UNKNOWN_LABEL,
        )

        results[split] = {
            # Every prediction forced - comparable in kind to published
            # benchmark numbers, which do not abstain.
            "no_abstention": raw.to_dict(),
            # What the pipeline would actually serve at this threshold.
            "with_abstention": gated.to_dict(),
            "most_confused": raw.most_confused(10),
        }

        print(f"===== {split} (no abstention) =====")
        print(render(raw))
        print()

    print("===== test confusion matrix (no abstention) =====")
    print(render_confusion(evaluate(truth["test"], model.predict(texts["test"]),
                                    list(LABELS))))
    print()

    # --- Artifact ------------------------------------------------------
    output = args.output_dir / model_name / version_label
    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "model.joblib"

    # Only sklearn objects and builtins: loadable without importing `ai`.
    joblib.dump(
        {
            "model": model,
            "labels": list(LABELS),
            "unknown_label": UNKNOWN_LABEL,
            "threshold": threshold,
            "preprocessing": preprocessing,
            "model_name": model_name,
            "model_version": model_version,
        },
        model_path,
    )

    metadata = {
        "model_name": model_name,
        "model_version": model_version,
        "architecture": config["architecture"],
        "framework": f"scikit-learn-{__import__('sklearn').__version__}",
        "labels": list(LABELS),
        "unknown_label": UNKNOWN_LABEL,
        "preprocessing": preprocessing,
        "features": config["features"],
        "classifier": config["classifier"],
        "vocabulary_size": vocabulary,
        "dataset": {
            "name": config["dataset"]["name"],
            "manifest_dir": str(manifest_dir),
            "per_class_cap": per_class,
            "samples": {split: len(samples[split]) for split in SPLITS},
            "empty_inputs": empty_counts,
        },
        "abstention": selection,
        "smoke_run": args.smoke,
        "training_seconds": round(training_seconds, 1),
        "loading_seconds": round(loading_seconds, 1),
        "inference_ms_per_document": measure_latency(model, texts["test"]),
        "model_size_bytes": model_path.stat().st_size,
        # Only a run on the official 320k split may be quoted against
        # published RVL-CDIP results. A subset is a different experiment.
        "comparable_to_published_rvlcdip": len(samples["train"]) >= OFFICIAL_TRAIN_SIZE,
    }

    (output / "metrics.json").write_text(json.dumps(results, indent=2))
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    (output / "labels.json").write_text(json.dumps(list(LABELS), indent=2))

    print(f"artifact: {model_path} "
          f"({metadata['model_size_bytes'] / 1e6:.1f} MB), "
          f"{metadata['inference_ms_per_document']} ms/document")
    print(f"comparable to published RVL-CDIP results: "
          f"{metadata['comparable_to_published_rvlcdip']}")


if __name__ == "__main__":
    main()
