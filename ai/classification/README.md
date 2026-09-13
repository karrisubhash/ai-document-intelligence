# Document classification

Training and evaluation for the 16-class document classifier. This
package does not import Django, and Django does not import it. The only
handoff is a versioned model artifact.

## Status

**Batch 1 (datasets) and the text baseline are complete.** The text
baseline is trained and evaluated on a 500/class QS-OCR subset; see
[Results](#results). The vision baseline has a working Kaggle GPU path
but only a smoke artifact — it is not trained.

## Quick start

```bash
export PYTHONPATH=<repo root>

# Download QS-OCR (252 MB) and build a balanced 500/class subset
python -m ai.classification.datasets.qs_ocr --per-class 500

# Gate before training
python -m ai.classification.datasets.integrity \
    ai/classification/data/manifests/qs_ocr --sample-limit 0

# Train the text baseline (~40 s end to end on 4 cores)
python -m ai.classification.training.train_text

# Tests
python -m unittest discover -s ai/classification/tests -t .
```

## Datasets

Every adapter emits the same `DatasetSample` and the same manifest CSV
(`sample_id,split,label,input_type,input_path`), so training code never
learns where the data came from.

| Source | Modality | Size | Notes |
|---|---|---|---|
| `qs_ocr` | text | **252 MB** | Tesseract OCR of all 400k RVL-CDIP pages |
| `rvl_cdip` | image | 38.76 GB | Single tarball, no partial download |

`sample_id` is the path with the extension stripped, which is identical
for a page's image and its OCR text. That is what will let the text and
vision baselines be compared on *the same samples* rather than on two
loosely-related subsets.

RVL-CDIP's split files are small and fetched separately, so manifests
can be built and inspected without downloading 38.76 GB of images.

## Results

Text baseline, `document-classifier v0.1.0` — TF-IDF (1-2 grams, 76,883
features) into a balanced logistic regression, trained on 8,000 QS-OCR
documents and evaluated once on 2,000 held-out ones.

| | validation | test |
|---|---|---|
| accuracy (every document answered) | 0.7050 | **0.7185** |
| macro-F1 | 0.7129 | **0.7276** |
| accuracy when answering (threshold 0.30) | 0.9223 | **0.9140** |
| coverage at that threshold | 51.5% | 51.7% |

Fits in 15.9 s, predicts in **1.02 ms/document**, artifact is 12.8 MB.

**These numbers are not comparable to published RVL-CDIP results.** They
come from a 500/class subset, not the official 320k/40k/40k split, and
the artifact metadata records `comparable_to_published_rvlcdip: false`.
They are also not a claim about college documents.

### Abstention

The threshold is fitted on validation to reach 90% accuracy among the
documents it chooses to answer, then applied unchanged to test. It
transfers: 92.2% at 51.5% coverage on validation, 91.4% at 51.7% on
test. The whole sweep it was chosen from is in `metrics.json`, so the
trade-off is auditable rather than asserted:

| threshold | coverage | accuracy when answering |
|---|---|---|
| 0.15 | 92.8% | 73.7% |
| 0.25 | 66.7% | 83.4% |
| **0.30** | **51.5%** | **92.2%** |
| 0.40 | 39.7% | 96.9% |
| 0.50 | 30.7% | 97.7% |

Half the corpus goes unanswered at the operating point. That is the
honest cost of not being wrong, and it is why the number that matters
downstream is the pair, never the accuracy alone.

### The predicted failure held

The prediction recorded before training — that a text-only model cannot
see what OCR did not produce — is what the confusion matrix shows.
`file folder` became a garbage attractor: recall 0.904 on 0.387
precision, absorbing the classes with the least OCR text.

| worst (test F1) | | best (test F1) | |
|---|---|---|---|
| handwritten | 0.508 | resume | 0.931 |
| advertisement | 0.542 | email | 0.929 |
| file folder | 0.542 | specification | 0.898 |

Most confused pairs on test: advertisement → file folder (34.4% of
actual), handwritten → file folder (28.0%), budget → file folder
(14.4%), letter → memo (13.6%). The first three are the low-text
classes; the fourth is a genuine semantic overlap that more OCR text
would not fix.

This is the argument for the vision baseline, now measured rather than
assumed: the failures are concentrated where there is nothing for a
text model to read.

## Training on Kaggle GPU

There is no GPU on this machine, so vision training runs remotely and
only the artifact comes back. Kaggle takes one source file per kernel,
so the submitter vendors the shared modules into a single script at
submit time — one source of truth, no notebook drifting out of sync.

```bash
# One-off: credentials from https://www.kaggle.com/settings -> API
export KAGGLE_USERNAME=... KAGGLE_KEY=...      # or ~/.kaggle/kaggle.json

# Inspect exactly what would be sent, without pushing
python -m ai.classification.training.submit_kaggle --smoke --dry-run

# Cheap proof the whole path works (1 epoch, 20 images/class)
python -m ai.classification.training.submit_kaggle --smoke

# Real run
python -m ai.classification.training.submit_kaggle --epochs 5
```

The submitter pushes, polls to completion, and downloads `model.pt`,
`metrics.json`, `metadata.json` and `labels.json` into
`artifacts/<model-name>/v<version>/`.

Kaggle's RVL-CDIP datasets are community re-uploads whose layout is not
verified from here, so `train_vision.py` discovers the split files and
image root at runtime and fails with a directory listing rather than a
`FileNotFoundError` deep in a dataloader.

## Measured facts

Established by running commands on this machine, not assumed:

- **No GPU.** Intel UHD Graphics 620 only; no CUDA anywhere on host or
  container. Vision fine-tuning is blocked — see `configs/vision_baseline.yaml`.
- **OCR-ing the corpus ourselves is not viable.** 0.93 pages/s saturated
  (4 cores) → 320k images ≈ 95.6 hours. This is why a pre-OCR'd corpus
  matters: QS-OCR is 252 MB and already done.
- **TF-IDF + linear is comfortable.** 19.2 s / 471 MB at 20k documents,
  with macro-F1 unchanged between 20k and 80k.
- **2.42% of QS-OCR documents are empty**, and it is not uniform:

  | class | empty | median chars |
  |---|---|---|
  | file folder | 26.0% | 18 |
  | handwritten | 13.0% | 199 |
  | advertisement | 12.4% | 115 |
  | scientific publication | 1.2% | 3559 |
  | resume | 0.0% | 2201 |

  A text-only model cannot see what OCR did not produce, so these three
  classes are where it was predicted to fail. **The confusion matrix
  confirmed it** — see [The predicted failure held](#the-predicted-failure-held).

## Reporting rules

Results are **benchmark performance on the public dataset used** — never
a claim about college documents. Those need a separate, governed
evaluation set from the institution.

Subset-trained numbers are not comparable to published RVL-CDIP results,
which use the official 320k/40k/40k split. Published anchors are recorded
in `configs/text_baseline.yaml` for sanity-checking only.

Note also that the corpus itself carries roughly 12% label noise (the
`letter` class far worse), and meaningful test/train near-duplicate
overlap, so even published numbers are somewhat inflated.
