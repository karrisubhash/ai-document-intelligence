"""Vision baseline for RVL-CDIP, written to run inside a Kaggle GPU kernel.

Runs unchanged locally (slowly, on CPU) and on Kaggle. The evaluation
module is imported normally when the package is importable, and vendored
in ahead of this file when the submitter assembles a single-file kernel -
hence the guarded import below.

Nothing here is RVL-CDIP-specific beyond the label list: the data layout
is discovered at runtime, because the Kaggle mirrors are community
re-uploads whose internal structure has not been verified from here.
Rather than assume one, this fails loudly and prints what it did find.
"""

import argparse
import json
import platform
import time
from pathlib import Path

# Vendored ahead of this file when assembled into a Kaggle kernel.
try:
    from ai.classification.datasets.base import LABELS
    from ai.classification.evaluation.metrics import evaluate, render, render_confusion
except ImportError:  # pragma: no cover - vendored path
    pass


IMAGE_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
SPLIT_FILE_NAMES = {
    "train": ("train.txt", "text_train.txt"),
    "validation": ("val.txt", "validation.txt", "text_val.txt"),
    "test": ("test.txt", "text_test.txt"),
}


# --- data discovery ---------------------------------------------------------


def find_split_files(root: Path) -> dict:
    """Locate official split files anywhere under `root`."""
    found = {}
    for split, candidates in SPLIT_FILE_NAMES.items():
        for name in candidates:
            matches = sorted(root.rglob(name))
            if matches:
                found[split] = matches[0]
                break
    return found


def find_image_root(root: Path, sample_relative: str) -> Path:
    """Find the directory that `sample_relative` is actually relative to."""
    if (root / sample_relative).exists():
        return root

    head = Path(sample_relative).parts[0]
    for candidate in sorted(root.rglob(head)):
        if candidate.is_dir() and (candidate.parent / sample_relative).exists():
            return candidate.parent

    raise FileNotFoundError(
        f"Could not locate image root for {sample_relative!r} under {root}. "
        f"Top-level entries: {[p.name for p in sorted(root.iterdir())][:20]}"
    )


def describe_input(root: Path) -> str:
    lines = [f"input root: {root}"]
    for path in sorted(root.iterdir())[:15]:
        kind = "dir " if path.is_dir() else "file"
        lines.append(f"  {kind} {path.name}")
    return "\n".join(lines)


def load_split(split_file: Path, image_root: Path, limit_per_class: int | None):
    """Read '<relative path> <label index>' into (path, label_index) pairs."""
    per_class: dict = {}
    items = []

    for line in split_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        relative, _, raw_label = line.rpartition(" ")
        label = int(raw_label)

        if limit_per_class is not None:
            if per_class.get(label, 0) >= limit_per_class:
                continue
            per_class[label] = per_class.get(label, 0) + 1

        items.append((image_root / relative, label))

    return items


# --- training ---------------------------------------------------------------


def build_model(architecture: str, num_classes: int):
    import torch.nn as nn
    import torchvision

    factory = getattr(torchvision.models, architecture, None)
    if factory is None:
        raise ValueError(f"unknown torchvision architecture {architecture!r}")

    model = factory(weights="DEFAULT")

    # Swap whichever head this family uses for a 16-way one.
    if hasattr(model, "fc"):
        model.fc = nn.Linear(model.fc.in_features, num_classes)
    elif hasattr(model, "classifier"):
        classifier = model.classifier
        if isinstance(classifier, nn.Sequential):
            for index in range(len(classifier) - 1, -1, -1):
                if isinstance(classifier[index], nn.Linear):
                    classifier[index] = nn.Linear(
                        classifier[index].in_features, num_classes
                    )
                    break
        else:
            model.classifier = nn.Linear(classifier.in_features, num_classes)
    elif hasattr(model, "heads"):
        model.heads.head = nn.Linear(model.heads.head.in_features, num_classes)
    else:
        raise ValueError(f"cannot locate classifier head on {architecture!r}")

    return model


def make_loaders(train_items, eval_items, image_size, batch_size, workers):
    import torch
    from PIL import Image
    from torch.utils.data import DataLoader, Dataset
    from torchvision import transforms

    # Documents are grayscale scans; replicate to 3 channels for
    # ImageNet-pretrained weights rather than retraining the stem.
    common = [
        transforms.Grayscale(num_output_channels=3),
        transforms.Resize((image_size, image_size)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ]

    class DocumentDataset(Dataset):
        def __init__(self, items, pipeline):
            self.items = items
            self.pipeline = transforms.Compose(pipeline)

        def __len__(self):
            return len(self.items)

        def __getitem__(self, index):
            path, label = self.items[index]
            with Image.open(path) as image:
                return self.pipeline(image.convert("L")), label

    def loader(items, shuffle):
        return DataLoader(
            DocumentDataset(items, common),
            batch_size=batch_size,
            shuffle=shuffle,
            num_workers=workers,
            pin_memory=torch.cuda.is_available(),
            drop_last=False,
        )

    return loader(train_items, True), {
        name: loader(items, False) for name, items in eval_items.items()
    }


def run_epoch(model, loader, device, optimizer=None, scaler=None):
    import torch
    import torch.nn as nn

    training = optimizer is not None
    model.train(training)
    criterion = nn.CrossEntropyLoss()

    total_loss, seen = 0.0, 0
    predictions, truths = [], []

    with torch.set_grad_enabled(training):
        for images, labels in loader:
            images = images.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)

            if training:
                optimizer.zero_grad(set_to_none=True)

            with torch.autocast(
                device_type=device.type, enabled=(device.type == "cuda")
            ):
                logits = model(images)
                loss = criterion(logits, labels)

            if training:
                if scaler is not None:
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    loss.backward()
                    optimizer.step()

            total_loss += loss.item() * labels.size(0)
            seen += labels.size(0)
            predictions.extend(logits.argmax(1).cpu().tolist())
            truths.extend(labels.cpu().tolist())

    return total_loss / max(1, seen), truths, predictions


def measure_latency(model, device, image_size, iterations=50):
    import torch

    model.eval()
    batch = torch.randn(1, 3, image_size, image_size, device=device)

    with torch.no_grad():
        for _ in range(10):
            model(batch)
        if device.type == "cuda":
            torch.cuda.synchronize()

        started = time.perf_counter()
        for _ in range(iterations):
            model(batch)
        if device.type == "cuda":
            torch.cuda.synchronize()
        elapsed = time.perf_counter() - started

    return (elapsed / iterations) * 1000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", default="/kaggle/input")
    parser.add_argument("--output-dir", default="/kaggle/working")
    parser.add_argument("--arch", default="resnet50")
    parser.add_argument("--model-version", default="0.1.0")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--per-class",
        type=int,
        default=None,
        help="Cap training samples per class. Omit for the full split.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Tiny run that proves the whole path works before spending GPU hours.",
    )
    args = parser.parse_args()

    if args.smoke:
        args.epochs, args.per_class = 1, 20

    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    print(f"python {platform.python_version()} | torch {torch.__version__}")
    print(f"device: {device}"
          f"{' - ' + torch.cuda.get_device_name(0) if device.type == 'cuda' else ''}")

    root = Path(args.input_root)
    print(describe_input(root))

    split_files = find_split_files(root)
    if "train" not in split_files or "test" not in split_files:
        raise SystemExit(
            f"Could not find split files under {root}. Found: {split_files}. "
            f"Attach a dataset that ships train.txt/val.txt/test.txt."
        )
    print(f"split files: { {k: str(v) for k, v in split_files.items()} }")

    first_line = split_files["train"].read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()[0]
    image_root = find_image_root(root, first_line.rsplit(" ", 1)[0])
    print(f"image root: {image_root}")

    train_items = load_split(split_files["train"], image_root, args.per_class)
    eval_limit = None if args.per_class is None else max(1, args.per_class // 4)
    eval_items = {
        name: load_split(split_files[name], image_root, eval_limit)
        for name in ("validation", "test")
        if name in split_files
    }
    print(f"train {len(train_items)} | "
          f"{ {k: len(v) for k, v in eval_items.items()} }")

    train_loader, eval_loaders = make_loaders(
        train_items, eval_items, args.image_size, args.batch_size, args.workers
    )

    model = build_model(args.arch, len(LABELS)).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    scaler = torch.amp.GradScaler(enabled=(device.type == "cuda"))

    history, training_started = [], time.time()
    for epoch in range(1, args.epochs + 1):
        epoch_started = time.time()
        loss, _, _ = run_epoch(model, train_loader, device, optimizer, scaler)
        schedule.step()

        entry = {"epoch": epoch, "train_loss": round(loss, 4),
                 "seconds": round(time.time() - epoch_started, 1)}

        if "validation" in eval_loaders:
            _, truth, predicted = run_epoch(model, eval_loaders["validation"], device)
            report = evaluate(
                [LABELS[i] for i in truth], [LABELS[i] for i in predicted], list(LABELS)
            )
            entry["val_accuracy"] = round(report.accuracy, 4)
            entry["val_macro_f1"] = round(report.macro_f1, 4)

        history.append(entry)
        print(f"epoch {epoch}/{args.epochs}: {entry}")

    training_seconds = time.time() - training_started

    results = {}
    for name, loader in eval_loaders.items():
        _, truth, predicted = run_epoch(model, loader, device)
        report = evaluate(
            [LABELS[i] for i in truth], [LABELS[i] for i in predicted], list(LABELS)
        )
        results[name] = report.to_dict()
        print(f"\n===== {name} =====\n{render(report)}")
        if name == "test":
            print(f"\n{render_confusion(report)}")

    weights_path = output / "model.pt"
    torch.save({"architecture": args.arch, "labels": list(LABELS),
                "state_dict": model.state_dict()}, weights_path)

    metadata = {
        "model_name": "document-classifier-vision",
        "model_version": args.model_version,
        "architecture": args.arch,
        "framework": f"torch-{torch.__version__}",
        "device": str(device),
        "gpu": torch.cuda.get_device_name(0) if device.type == "cuda" else None,
        "labels": list(LABELS),
        "image_size": args.image_size,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "learning_rate": args.lr,
        "per_class_cap": args.per_class,
        "smoke_run": args.smoke,
        "train_samples": len(train_items),
        "eval_samples": {k: len(v) for k, v in eval_items.items()},
        "training_seconds": round(training_seconds, 1),
        "inference_ms_per_image": round(
            measure_latency(model, device, args.image_size), 3
        ),
        "model_size_bytes": weights_path.stat().st_size,
        "history": history,
        # A subset run is a different experiment from the published
        # benchmark and must not be quoted against it.
        "comparable_to_published_rvlcdip": args.per_class is None and not args.smoke,
    }

    (output / "metrics.json").write_text(json.dumps(results, indent=2))
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    (output / "labels.json").write_text(json.dumps(list(LABELS), indent=2))

    print(f"\nartifact: {weights_path} "
          f"({metadata['model_size_bytes'] / 1e6:.1f} MB), "
          f"{metadata['inference_ms_per_image']} ms/image")
    print(f"comparable to published RVL-CDIP results: "
          f"{metadata['comparable_to_published_rvlcdip']}")


if __name__ == "__main__":
    main()
