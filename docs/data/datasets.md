# Datasets

No institutional data is in this repository, and none has been fabricated. Every dataset
below is either public, or synthetic and labelled as such. Each has a card: provenance,
purpose, version, schema, split and role.

## Institutional evaluation corpus (synthetic)

| | |
|---|---|
| Location | `evaluation/corpus/*.txt` (9 documents) |
| Provenance | **Synthetic.** Written for this repository to resemble institutional regulations — academic regulations, examination policy, fee structure, hostel, library, grievance procedure, IT acceptable use, placement policy, scholarship rules. Not copied from any real institution. |
| Purpose | Regression-testing retrieval and answering end to end, through the real ingestion pipeline |
| Deliberate properties | Overlapping vocabulary across documents (`15 days` is both a revaluation deadline and a grievance window; `condonation` appears in three documents; attendance thresholds of 75 % and 85 % mean different things), a table (fee structure), numbered clauses |
| Versioning | Governed through the benchmark templates below; editing a corpus file changes what a benchmark version measures, so it requires a new benchmark version |
| Use | Evaluation and end-to-end tests only. **A score on it says the pipeline works, not that it works on a real institution's documents.** |

## Governed benchmarks

| | `institutional-seed` v1 | `institutional-v2` v2 |
|---|---|---|
| Template | `evaluation/benchmark.template.json` | `evaluation/benchmark-v2.template.json` |
| Cases | 13 over 5 documents | 57 over 9 documents (includes v1's) |
| Case types | direct lookups, a table lookup, negatives, questions that must be declined | adds **paraphrase** cases that avoid the source's vocabulary and **near-miss** cases where two documents share a term |
| Schema per case | `question`, `expected_answer` (or an expected abstention), `relevant_documents` (corpus slugs), `relevant_pages`, `notes` |
| Split | Test-only. There is no training split, and no component is tuned on these cases: thresholds come from the unit-tested rules, and the benchmark only measures them |
| Immutability | Loaded datasets are immutable (`EvaluationDataset`); a change is a new version, never an edit, so scores across time compare like with like |
| Resolution | `manage.py seed_evaluation` ingests the corpus through the real pipeline and resolves slugs to database ids into `evaluation/benchmark*.json`, which is git-ignored because ids are per-database |

`evaluation/baseline.json` is the committed v1 baseline the CI gate compares against. See
[evaluation/README.md](../evaluation/README.md).

## Public classification data

| | QS-OCR | RVL-CDIP |
|---|---|---|
| Source | <https://github.com/Quicksign/ocrized-text-dataset> (release v1.0) | <https://huggingface.co/datasets/aharley/rvl_cdip> |
| Content | Tesseract OCR text of all 400,000 RVL-CDIP pages | 400,000 scanned document images in 16 classes (letter, memo, invoice, form, …) |
| Size | 252 MB | 38.76 GB |
| Local copy | `ai/classification/data/` (git-ignored) | split files only; images not downloaded |
| Split | Official train/validation/test; the text baseline uses a hash-selected, class-balanced 500/class subset of the official splits | official |
| Role | Training and evaluating the text classifier (`document-classifier v0.1.0`, test accuracy 0.7185 on 2,000 held-out pages) | Vision baseline path (smoke artifact only — **not trained**) |
| Caveat | **These classes are archival business-document types, not institutional ones.** The classifier's labels (letter, invoice, …) do not map onto regulation/policy/syllabus; on the academic-regulations document in the end-to-end run it answered `unknown` at 0.13 confidence rather than forcing a label, which is the honest outcome — but one observation is not a measurement. An institutional classifier needs labelled institutional data. |
| Terms | Use is subject to each source's own terms; check them before any redistribution |

`manage.py ingest_corpus --source qs-ocr` can ingest the QS-OCR pages through the real
pipeline for scale testing, spread across departments by class. It writes a gold manifest
(`evaluation/corpus_manifest.json`, git-ignored) keyed by document id.

## When institutional data becomes available

Nothing in the platform assumes the synthetic corpus. To evaluate on real documents:

1. Create a new benchmark template with its own name and version, whose cases reference
   the real documents by slug, written and reviewed by people who know the answers.
2. Record its provenance and access restrictions in a card on this page; keep the resolved
   file out of version control if the questions themselves are sensitive.
3. Scope it to the owning department (`EvaluationDataset.department`) — evaluation runs are
   visible only to that department and system administrators, because a benchmark reveals
   its corpus through its questions.
4. Establish a baseline from one run, commit it, and gate on it.

A classifier for institutional document types needs a labelled set of the same kind,
split by document rather than by page, before any accuracy is claimed.
