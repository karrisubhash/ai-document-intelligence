"""QS-OCR-Large: Tesseract OCR text of RVL-CDIP.

Quicksign released the OCR output of every RVL-CDIP page as plain text,
preserving the original folder structure and the official splits. At
252 MB for 399,999 documents - against 38.76 GB for the images - this is
what makes a text baseline tractable on a machine with no GPU.

The archive holds 400,002 members: the documents plus text_train.txt,
text_val.txt and text_test.txt at the root. Manifests are built by
reading those split files straight out of the tarball, and only the
selected samples are then extracted - unpacking all 400k tiny files
costs far more than a subset run needs.

Text is raw Tesseract output; the publishers deliberately did not
post-process it, and 2.42% of files are empty. Cleaning belongs in
preprocessing/, and the empties are the integrity checker's business.
An adapter reports what exists, it does not repair it.

Source: https://github.com/Quicksign/ocrized-text-dataset (release v1.0)
"""

import argparse
import tarfile
from pathlib import Path

from ai.common.download import download

from .base import (
    SPLITS,
    DatasetSample,
    balanced_subset,
    parse_split_lines,
    write_manifest,
)

NAME = "qs-ocr-large"
VERSION = "v1.0"
ARCHIVE_URL = (
    "https://github.com/QuickSign/ocrized-text-dataset/releases/download/"
    "v1.0/QS-OCR-Large.tar.gz"
)
ARCHIVE_NAME = "QS-OCR-Large.tar.gz"

SPLIT_FILES = {
    "train": "text_train.txt",
    "validation": "text_val.txt",
    "test": "text_test.txt",
}


def read_split_files(archive: Path) -> dict[str, str]:
    """Pull the three split files out of the tarball without unpacking it."""
    wanted = {name: split for split, name in SPLIT_FILES.items()}
    contents: dict[str, str] = {}

    with tarfile.open(archive) as tar:
        for member in tar:
            if member.name not in wanted:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            contents[wanted[member.name]] = handle.read().decode("utf-8", "replace")
            if len(contents) == len(wanted):
                break

    missing = set(SPLIT_FILES) - set(contents)
    if missing:
        raise FileNotFoundError(
            f"{archive} is missing split files for {sorted(missing)}; "
            f"expected {sorted(SPLIT_FILES.values())} at the archive root."
        )

    return contents


def extract_members(archive: Path, wanted: set[str], destination: Path) -> int:
    """Extract only the named members, in a single sequential pass."""
    destination.mkdir(parents=True, exist_ok=True)
    remaining = {name for name in wanted if not (destination / name).exists()}

    if not remaining:
        print(f"  all {len(wanted)} files already extracted")
        return 0

    extracted = 0
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.name not in remaining:
                continue
            # filter="data" refuses absolute paths and traversal entries.
            tar.extract(member, destination, filter="data")
            extracted += 1
            if extracted % 2000 == 0:
                print(f"  extracted {extracted}/{len(remaining)}")

    print(f"  extracted {extracted} file(s) to {destination}")
    return extracted


def prepare(
    data_dir: Path,
    manifest_dir: Path,
    *,
    per_class: int | None = None,
    seed: int = 0,
    extract: bool = True,
) -> dict[str, int]:
    archive = download(ARCHIVE_URL, data_dir / ARCHIVE_NAME)
    root = data_dir / "QS-OCR-Large"

    print("  reading split files from archive")
    split_text = read_split_files(archive)

    selected: dict[str, list[DatasetSample]] = {}
    for split in SPLITS:
        samples = parse_split_lines(
            split_text[split], split, root, "text", source=SPLIT_FILES[split]
        )

        if per_class is not None:
            # Validation and test stay proportionally smaller so a quick
            # run does not spend its budget on evaluation.
            budget = per_class if split == "train" else max(1, per_class // 4)
            samples = balanced_subset(samples, budget, seed=seed)

        selected[split] = samples

    if extract:
        wanted = {
            str(Path(s.input_path).relative_to(root)).replace("\\", "/")
            for samples in selected.values()
            for s in samples
        }
        print(f"  extracting {len(wanted)} selected document(s)")
        extract_members(archive, wanted, root)

    counts = {}
    for split, samples in selected.items():
        counts[split] = write_manifest(samples, manifest_dir / f"{split}.csv")
        print(f"  {split}: {counts[split]} samples -> {manifest_dir / f'{split}.csv'}")

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("ai/classification/data/qs_ocr"))
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path("ai/classification/data/manifests/qs_ocr"),
    )
    parser.add_argument(
        "--per-class",
        type=int,
        default=None,
        help="Balanced samples per class for train; validation/test get a "
        "quarter each. Omit for the full 399,999 documents.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Build manifests only, without unpacking documents.",
    )
    args = parser.parse_args()

    print(f"Preparing {NAME} {VERSION}")
    counts = prepare(
        args.data_dir,
        args.manifest_dir,
        per_class=args.per_class,
        seed=args.seed,
        extract=not args.no_extract,
    )
    print(f"Done: {sum(counts.values())} samples across {len(counts)} splits.")


if __name__ == "__main__":
    main()
