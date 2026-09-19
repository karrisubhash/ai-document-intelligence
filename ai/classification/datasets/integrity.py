"""Dataset integrity checks, run before anything is trained.

A silent split leak or a class with 3 samples produces a number that
looks like a result and isn't one. These checks are meant to fail loudly
and early rather than let that happen.
"""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .base import LABELS, SPLITS, DatasetSample, read_manifest


@dataclass
class IntegrityReport:
    splits: dict = field(default_factory=dict)
    class_distribution: dict = field(default_factory=dict)
    duplicates: dict = field(default_factory=dict)
    leakage: dict = field(default_factory=dict)
    missing_files: dict = field(default_factory=dict)
    unreadable_files: dict = field(default_factory=dict)
    empty_inputs: dict = field(default_factory=dict)
    unknown_labels: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "splits": self.splits,
            "class_distribution": self.class_distribution,
            "duplicates": self.duplicates,
            "leakage": self.leakage,
            "missing_files": self.missing_files,
            "unreadable_files": self.unreadable_files,
            "empty_inputs": self.empty_inputs,
            "unknown_labels": self.unknown_labels,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _inspect(sample: DatasetSample) -> tuple[str | None, bool]:
    """Returns (unreadable_reason, is_empty)."""
    path = Path(sample.input_path)
    try:
        if sample.input_type == "text":
            content = path.read_text(encoding="utf-8", errors="strict")
            return None, not content.strip()

        with path.open("rb") as handle:
            return None, not handle.read(16)
    except UnicodeDecodeError:
        return "not valid utf-8", False
    except OSError as exc:
        return str(exc), False


def check(
    manifest_dir: Path,
    *,
    check_files: bool = True,
    sample_limit: int | None = 2000,
) -> IntegrityReport:
    report = IntegrityReport()
    by_split: dict[str, list[DatasetSample]] = {}

    for split in SPLITS:
        path = manifest_dir / f"{split}.csv"
        if not path.exists():
            report.errors.append(f"missing manifest: {path}")
            continue
        by_split[split] = read_manifest(path)

    if not by_split:
        report.errors.append(f"no manifests found under {manifest_dir}")
        return report

    seen_ids: dict[str, set[str]] = {}

    for split, samples in by_split.items():
        report.splits[split] = len(samples)

        # --- class distribution -------------------------------------
        distribution = Counter(sample.label for sample in samples)
        report.class_distribution[split] = dict(sorted(distribution.items()))

        unknown = sorted(set(distribution) - set(LABELS))
        if unknown:
            report.unknown_labels[split] = unknown
            report.errors.append(f"{split}: labels outside the schema: {unknown}")

        absent = [label for label in LABELS if label not in distribution]
        if absent:
            report.warnings.append(f"{split}: no samples for {absent}")

        if distribution:
            smallest, largest = min(distribution.values()), max(distribution.values())
            if largest and smallest / largest < 0.5:
                report.warnings.append(
                    f"{split}: class imbalance - smallest {smallest}, largest {largest}"
                )

        # --- duplicates within the split ----------------------------
        ids = [sample.sample_id for sample in samples]
        repeated = {i: c for i, c in Counter(ids).items() if c > 1}
        if repeated:
            report.duplicates[split] = len(repeated)
            report.errors.append(
                f"{split}: {len(repeated)} duplicated sample_id(s), "
                f"e.g. {sorted(repeated)[:3]}"
            )

        seen_ids[split] = set(ids)

    # --- split leakage ----------------------------------------------
    for left in by_split:
        for right in by_split:
            if left >= right:
                continue
            shared = seen_ids[left] & seen_ids[right]
            if shared:
                report.leakage[f"{left}|{right}"] = len(shared)
                report.errors.append(
                    f"leakage: {len(shared)} sample(s) in both {left} and {right}, "
                    f"e.g. {sorted(shared)[:3]}"
                )

    # --- files on disk ----------------------------------------------
    if check_files:
        for split, samples in by_split.items():
            subset = samples if sample_limit is None else samples[:sample_limit]
            missing, unreadable, empty = [], defaultdict(list), []

            for sample in subset:
                if not Path(sample.input_path).exists():
                    missing.append(sample.sample_id)
                    continue
                reason, is_empty = _inspect(sample)
                if reason:
                    unreadable[reason].append(sample.sample_id)
                elif is_empty:
                    empty.append(sample.sample_id)

            if empty:
                # Inherent to the corpus (OCR genuinely found nothing on
                # some pages), so it is surfaced rather than failed on -
                # but preprocessing must have a strategy for these.
                report.empty_inputs[split] = {
                    "checked": len(subset),
                    "empty": len(empty),
                    "share": round(len(empty) / len(subset), 4),
                    "examples": empty[:3],
                }
                report.warnings.append(
                    f"{split}: {len(empty)}/{len(subset)} inputs have no text "
                    f"({100 * len(empty) / len(subset):.2f}%)"
                )

            if missing:
                report.missing_files[split] = {
                    "checked": len(subset),
                    "missing": len(missing),
                    "examples": missing[:3],
                }
                report.errors.append(
                    f"{split}: {len(missing)}/{len(subset)} input files missing"
                )

            if unreadable:
                report.unreadable_files[split] = {
                    reason: len(ids) for reason, ids in unreadable.items()
                }
                report.errors.append(
                    f"{split}: {sum(len(v) for v in unreadable.values())} unreadable file(s)"
                )

            if sample_limit is not None and len(samples) > sample_limit:
                report.warnings.append(
                    f"{split}: file checks sampled the first {sample_limit} of "
                    f"{len(samples)} rows (raise --sample-limit for a full sweep)"
                )

    return report


def render(report: IntegrityReport) -> str:
    lines = []
    lines.append(
        f"{'split':<12}{'samples':>10}{'classes':>10}{'min/class':>12}{'max/class':>12}"
    )
    lines.append("-" * 56)

    for split, total in report.splits.items():
        distribution = report.class_distribution.get(split, {})
        values = distribution.values() or [0]
        lines.append(
            f"{split:<12}{total:>10}{len(distribution):>10}"
            f"{(min(values) if distribution else 0):>12}"
            f"{(max(values) if distribution else 0):>12}"
        )

    lines.append("")
    lines.append(f"duplicates    : {report.duplicates or 'none'}")
    lines.append(f"split leakage : {report.leakage or 'none'}")
    lines.append(f"missing files : {report.missing_files or 'none'}")
    lines.append(f"unreadable    : {report.unreadable_files or 'none'}")
    lines.append(f"empty inputs  : {report.empty_inputs or 'none'}")

    for warning in report.warnings:
        lines.append(f"WARN  {warning}")
    for error in report.errors:
        lines.append(f"ERROR {error}")

    lines.append("")
    lines.append("PASS" if report.ok else "FAIL")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest_dir", type=Path)
    parser.add_argument("--json", dest="json_path", type=Path, default=None)
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=2000,
        help="Per-split cap on file existence/readability checks. 0 = all.",
    )
    parser.add_argument("--skip-files", action="store_true")
    args = parser.parse_args()

    report = check(
        args.manifest_dir,
        check_files=not args.skip_files,
        sample_limit=None if args.sample_limit == 0 else args.sample_limit,
    )
    print(render(report))

    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report.to_dict(), indent=2))
        print(f"\nreport written to {args.json_path}")

    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
