# AI development workspace

Training and evaluation live here. Django does not import from this
package, and this package does not import Django.

```
ai/
├── classification/
│   ├── datasets/       dataset acquisition + reproducible splits
│   ├── preprocessing/  raw dataset -> model-ready features
│   ├── training/       training entrypoints + configs
│   ├── evaluation/     metrics, confusion matrix, latency/memory
│   └── models/         model artifacts (git-ignored) + model cards
└── common/             shared helpers
```

The handoff between the two sides is a **model artifact plus a version
string**, not a shared codebase:

```
ai/  training + evaluation
        ↓
   model artifact (name + version)
        ↓
backend/documents/processing/classification/   inference adapter
        ↓
   Django pipeline -> ClassificationResult
```

An adapter in `backend/documents/processing/classification/` implements
the `DocumentClassifier` protocol and is pointed at by the
`DOCUMENT_CLASSIFIER` Django setting. When that setting is unset, the
CLASSIFICATION pipeline stage records SKIPPED — it never guesses a label.

## Status

Dataset selection and baseline choice are **pending the feasibility
report**. Nothing here is trained yet, and no accuracy has been claimed.

Any results produced here will be reported as *benchmark performance on
the public dataset used*, never as performance on college documents —
those are different claims and require a separate, governed evaluation
set supplied by the institution.
