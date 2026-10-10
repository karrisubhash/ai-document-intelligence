"""RVL-CDIP: 400,000 document page images across 16 classes.

The official splits (320k/40k/40k) ship as small text files that can be
fetched on their own, but the images themselves are a single 38.76 GB
tarball - there is no partial download. So manifest building and image
acquisition are separate steps: you can plan, subset and inspect the
dataset for free, and only pay the 38.76 GB when a vision baseline
actually needs pixels.

Source: https://huggingface.co/datasets/aharley/rvl_cdip
"""

import argparse
from pathlib import Path

from ai.common.download import download, extract_tar

from .base import (
    LABELS,
    balanced_subset,
    parse_split_lines,
    write_manifest,
)

NAME = "rvl-cdip"
VERSION = "aharley/rvl_cdip@main"
BASE_URL = "https://huggingface.co/datasets/aharley/rvl_cdip/resolve/main/data"
IMAGES_ARCHIVE = "rvl-cdip.tar.gz"
IMAGES_BYTES = 38_760_000_000
# The tarball's top-level directory. Split-file paths (imagesq/q/o/c/...)
# are relative to it, not to the directory the tarball is unpacked into.
IMAGES_DIR = "images"

SPLIT_FILES = {"train": "train.txt", "validation": "val.txt", "test": "test.txt"}


def download_split_files(data_dir: Path) -> Path:
    """Fetch just the label files - a few MB, no images."""
    for filename in SPLIT_FILES.values():
        download(f"{BASE_URL}/{filename}", data_dir / filename)
    return data_dir


def download_images(data_dir: Path) -> Path:
    """Fetch and unpack the full image tarball. This is 38.76 GB."""
    print(
        f"  WARNING: downloading {IMAGES_BYTES / 1e9:.1f} GB. "
        f"RVL-CDIP has no partial-download option."
    )
    archive = download(f"{BASE_URL}/{IMAGES_ARCHIVE}", data_dir / IMAGES_ARCHIVE)
    return extract_tar(archive, data_dir, marker=IMAGES_DIR)


def prepare(
    data_dir: Path,
    manifest_dir: Path,
    *,
    per_class: int | None = None,
    seed: int = 0,
    with_images: bool = False,
) -> dict[str, int]:
    download_split_files(data_dir)

    if with_images:
        download_images(data_dir)

    counts = {}
    for split, filename in SPLIT_FILES.items():
        # Identical line format to QS-OCR ('<relative path> <label index>'),
        # so the parser is shared rather than duplicated.
        samples = parse_split_lines(
            (data_dir / filename).read_text(encoding="utf-8", errors="replace"),
            split,
            data_dir / IMAGES_DIR,
            "image",
            source=filename,
        )

        if per_class is not None:
            budget = per_class if split == "train" else max(1, per_class // 4)
            samples = balanced_subset(samples, budget, seed=seed)

        counts[split] = write_manifest(samples, manifest_dir / f"{split}.csv")
        print(f"  {split}: {counts[split]} samples -> {manifest_dir / f'{split}.csv'}")

    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path("ai/classification/data/rvl_cdip")
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=Path("ai/classification/data/manifests/rvl_cdip"),
    )
    parser.add_argument("--per-class", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--with-images",
        action="store_true",
        help=f"Also download the {IMAGES_BYTES / 1e9:.0f} GB image tarball.",
    )
    args = parser.parse_args()

    print(f"Preparing {NAME} ({len(LABELS)} classes)")
    counts = prepare(
        args.data_dir,
        args.manifest_dir,
        per_class=args.per_class,
        seed=args.seed,
        with_images=args.with_images,
    )
    print(f"Done: {sum(counts.values())} samples.")
    if not args.with_images:
        print(
            "Manifests reference image paths that are not downloaded yet; "
            "run with --with-images before training a vision model."
        )


if __name__ == "__main__":
    main()
