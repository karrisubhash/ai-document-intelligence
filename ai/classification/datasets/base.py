"""The dataset contract every adapter produces.

Training code consumes DatasetSample and manifests. It never learns
where the data came from, so swapping RVL-CDIP images for QS-OCR text -
or adding a college corpus later - does not touch the training code.
"""

import csv
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Literal

# The canonical RVL-CDIP label order, index 0-15. Verified against
# aharley/rvl_cdip dataset_infos.json rather than recalled - the integer
# in the official split files indexes directly into this tuple.
LABELS = (
    "letter",
    "form",
    "email",
    "handwritten",
    "advertisement",
    "scientific report",
    "scientific publication",
    "specification",
    "file folder",
    "news article",
    "budget",
    "invoice",
    "presentation",
    "questionnaire",
    "resume",
    "memo",
)

LABEL_TO_INDEX = {label: index for index, label in enumerate(LABELS)}

TRAIN, VALIDATION, TEST = "train", "validation", "test"
SPLITS = (TRAIN, VALIDATION, TEST)

InputType = Literal["image", "text"]


@dataclass(frozen=True)
class DatasetSample:
    sample_id: str
    split: str
    label: str
    input_type: InputType
    input_path: str


MANIFEST_FIELDS = tuple(f.name for f in fields(DatasetSample))


def sample_id_from_path(relative_path: str) -> str:
    """Stable id shared across modalities.

    QS-OCR mirrors RVL-CDIP's folder structure, so dropping the suffix
    yields the same id for a page's image and its OCR text. That is what
    lets the text and vision baselines be compared on identical samples
    instead of on two loosely-related subsets.
    """
    cleaned = relative_path.strip().replace("\\", "/").lstrip("./")
    return cleaned.rsplit(".", 1)[0] if "." in cleaned.rsplit("/", 1)[-1] else cleaned


def parse_split_lines(
    text: str,
    split: str,
    root: Path,
    input_type: InputType,
    *,
    source: str = "<split file>",
) -> list[DatasetSample]:
    """Parse RVL-CDIP's '<relative path> <label index>' split format.

    QS-OCR reuses this format verbatim, so both adapters share it.
    """
    samples = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        relative_path, _, label_index = line.rpartition(" ")
        if not relative_path:
            raise ValueError(f"{source}:{line_number}: cannot parse {line!r}")

        try:
            index = int(label_index)
            if index < 0:
                # LABELS[-1] would silently read as "memo".
                raise IndexError(index)
            label = LABELS[index]
        except (ValueError, IndexError) as exc:
            raise ValueError(
                f"{source}:{line_number}: bad label index {label_index!r}"
            ) from exc

        samples.append(
            DatasetSample(
                sample_id=sample_id_from_path(relative_path),
                split=split,
                label=label,
                input_type=input_type,
                input_path=str(root / relative_path),
            )
        )

    return samples


def write_manifest(samples: Iterable[DatasetSample], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for sample in samples:
            writer.writerow(
                {field: getattr(sample, field) for field in MANIFEST_FIELDS}
            )
            count += 1

    return count


def read_manifest(path: Path) -> list[DatasetSample]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [DatasetSample(**row) for row in csv.DictReader(handle)]


def _selection_key(seed: int, sample_id: str) -> str:
    return hashlib.sha256(f"{seed}:{sample_id}".encode()).hexdigest()


def balanced_subset(
    samples: Iterable[DatasetSample],
    per_class: int,
    seed: int = 0,
) -> list[DatasetSample]:
    """A deterministic, class-balanced subset.

    Selection is by hash of (seed, sample_id) rather than a PRNG over
    input order, so the same subset comes back regardless of platform,
    Python version, or the order the adapter happened to yield samples.
    """
    by_label: dict[str, list[DatasetSample]] = {}
    for sample in samples:
        by_label.setdefault(sample.label, []).append(sample)

    selected: list[DatasetSample] = []
    for label in sorted(by_label):
        ranked = sorted(
            by_label[label], key=lambda s: _selection_key(seed, s.sample_id)
        )
        selected.extend(ranked[:per_class])

    return sorted(selected, key=lambda s: s.sample_id)
