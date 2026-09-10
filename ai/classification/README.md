# Document classification

Training and evaluation for the 16-class document classifier. This
package does not import Django, and Django does not import it. The only
handoff is a versioned model artifact.

## Status

**Batch 1 (datasets) is complete and verified.** Nothing is trained yet
and no accuracy is claimed.

## Quick start

```bash
export PYTHONPATH=<repo root>

# Download QS-OCR (252 MB) and build a balanced 500/class subset
python -m ai.classification.datasets.qs_ocr --per-class 500

# Gate before training
python -m ai.classification.datasets.integrity \
    ai/classification/data/manifests/qs_ocr --sample-limit 0

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
  classes are where it is predicted to fail. The confusion matrix will
  test that prediction.

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
