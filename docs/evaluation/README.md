# Evaluation

What is measured, how, on what data, and what the numbers currently are. Every number on
this page was produced by the commands described here on the dates given; nothing is
estimated. The datasets are **synthetic** (see [data/datasets.md](../data/datasets.md)): a
score here says the pipeline behaves as designed, not that it performs this well on a real
institution's documents.

## Method

```bash
python manage.py seed_evaluation --template <template> --output <resolved.json> --prune
python manage.py evaluate --load <resolved.json> --dataset <name> --dataset-version <v> \
    --as-user ci-evaluator --label <config> --write-baseline <out.json>
```

`seed_evaluation` ingests the corpus through the **real pipeline** (so extraction,
chunking and indexing are part of what is measured) and resolves the template's document
slugs to ids. `evaluate` asks every question as a real user through the full RAG engine and
stores per-case results (`EvaluationCaseResult`) beside the run's configuration.

| Metric | Definition (`documents/evaluation/metrics.py`) |
|---|---|
| Recall@k, Precision@k, MRR, MAP, nDCG@k | over **documents**, in reranked order, for answerable cases |
| Coverage | share of cases answered rather than declined |
| Abstention accuracy | share of cases where answering-vs-declining was the right call |
| False / missed abstentions | declined an answerable case / answered a case that should be declined |
| Citation accuracy | share of cited documents that are relevant (an answer citing nothing scores 0) |
| Source coverage | share of relevant documents the answer cited |
| Groundedness | mean grounding support ratio of answered cases |
| Token overlap | bag-of-words overlap with the reference answer — a weak signal, never reported as correctness |
| p50 / p95 / max latency | wall time per question inside the engine |

Loaded datasets are immutable: loading a changed version is refused, so a run always
answers the exam it says it answers.

## The regression gate

CI's `ai-regression` job seeds `institutional-seed` v1 with the offline baselines (hashing
embeddings, lexical reranking, extractive answers) and runs `evaluate --baseline
evaluation/baseline.json --fail-on-regression`; then it seeds v2 on top (its corpus is a
superset) and gates on `evaluation/baseline-v2.json` the same way. v1 alone cannot catch a
retrieval that got *worse* - every configuration scores recall@1 1.0 on it - and v2 can. Any quality metric below the committed
baseline (beyond `--tolerance`) fails the build; latency is reported but only gated with
`--gate-latency`.

**Measured 2026-10-05**, run exactly as the CI job runs it (a fresh database, the corpus
seeded through the real pipeline, offline baselines): no regression. Recall@1 1.000,
coverage 0.692, abstention accuracy 0.923, citation accuracy 0.944, as committed. Two values
rose - groundedness 0.9704 -> 0.9709 and token overlap 0.764 -> 0.931 (two answers now
match their reference more closely) - and the committed baseline was raised to them, so a
later change cannot quietly give the gain back. The offline pipeline is deterministic, so
the higher bar costs no flakiness.

## Results — configurations compared

Measured 2026-09-30 on the development machine (CPU only), each configuration on a
disposable database seeded through the real pipeline. Latencies were taken while other
workloads (an image build) shared the CPU — treat them as relative, not absolute.

### v1 (`institutional-seed`, 13 cases, 5 documents)

Every configuration scores Recall@1 = 1.000: with five documents whose vocabulary matches
the questions, v1 **cannot distinguish configurations**. It remains the CI gate because it
is fast and deterministic, not because it is discriminating.

### v2 (`institutional-v2`, 57 cases, 9 documents — adds paraphrase and near-miss cases)

| Configuration | R@1 | R@3 | MRR | nDCG@5 | Coverage | Abst. acc. | Citation acc. | False abst. | Missed abst. | p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hashing + lexical (offline default) | 0.796 | 0.837 | 0.860 | 0.879 | 0.597 | 0.597 | 0.794 | 19 | 4 | 41 ms | 62 ms |
| bge-small + lexical | 0.816 | 0.898 | 0.887 | 0.915 | 0.597 | 0.597 | 0.794 | 19 | 4 | 173 ms | 439 ms |
| hashing + bge-reranker-base | 0.857 | 0.980 | 0.926 | 0.945 | 0.491 | 0.561 | 0.929 | 23 | 2 | 6.8 s | 10.9 s |
| bge-small + bge-reranker-base | 0.857 | 0.980 | 0.926 | 0.945 | 0.491 | 0.561 | 0.929 | 23 | 2 | 6.2 s | 16.3 s |
| bge-small + MiniLM-L6 cross-encoder | **0.980** | **1.000** | **1.000** | **1.000** | 0.491 | 0.561 | 0.875 | 23 | 2 | 644 ms | 3.3 s |

All runs used the extractive answerer (no generative provider is configured on this
machine) and the relevance floor in force at the time: 0.15 lexical, 0.30 for both
cross-encoders. The cross-encoder floors are now calibrated per model; see
[calibration](#calibration).

### What the numbers say

**Retrieval.** Semantic embeddings help modestly on their own (MRR 0.860 → 0.887). A
cross-encoder helps a lot, and the small MiniLM model is the best on every retrieval
metric *and* about ten times faster than bge-reranker-base (and roughly a twelfth of its
size on disk).

**Answers.** Coverage does not follow retrieval, and the per-case abstention reasons show
exactly why. Of the 49 answerable cases:

| Configuration | Answered | Declined by the relevance floor | Declined by the answerer |
|---|---|---|---|
| lexical reranking | 30 | 1 | 18 |
| either cross-encoder | 26 | 16 | 7 |

- With lexical reranking the bottleneck is the **extractive answerer**: it quotes only
  sentences that share the question's words, so a paraphrased question over a correctly
  retrieved document is declined. A generative answerer is the component designed for
  this; none could be exercised here (no provider credentials).
- With a cross-encoder, the relevance floor declined 16 of them at 0.30. That default
  was chosen, never measured. Calibrating it showed the floor matters less than these
  counts suggest: lower it, and most of the same questions are declined by the
  answerer instead (see [calibration](#calibration)).
- The cross-encoders also halve missed abstentions (4 → 2) and raise citation accuracy:
  they decline more, but what they answer is better sourced.

The floor cannot be tuned on these 57 cases without tuning on the test set. See
[calibration](#calibration) for how it is being set instead.

## Calibration

The cross-encoder relevance floor (0.30) was a choice, never a measurement, and tuning it
on v2 would mean tuning on the test set. So it was chosen on a separate **development
set** and checked against v2 exactly once.

**The development set.** `evaluation/calibration-c1.template.json` (`institutional-calibration`
c1) has 37 synthetic questions over the same nine documents: 30 answerable (11 direct, 13
paraphrase, 6 near-miss) and 7 that must be declined. Each was written by one agent, checked
by a second that its answer is stated in the claimed document (or, for the 7, in none of the
nine), and screened by a third against the 57 v2 cases for leakage. A question was kept
only if both checks passed. 24 of the 61 authored questions were dropped, most for asking a
v2 fact from the same angle. It is development data, and its scores are never reported as
quality.

**The rule, written before any result was seen.** Choose the floor that maximises
abstention accuracy on the development set; break ties toward the higher (more
conservative) floor. Swept over 0.30 down to 0.001, with bge-small embeddings.

| Development set | Floor 0.30 | Best floor | At best floor |
|---|---|---|---|
| MiniLM-L6 | abstention accuracy 0.486 | **0.30** (ties at 0.01 and below) | unchanged |
| bge-reranker-base | 0.432 | **0.02** | 0.486; coverage 0.514 → 0.568 |

**Then v2, once, at those floors** (measured 2026-10-05, current code):

| Configuration | R@1 | MRR | Coverage | Abst. acc. | Citation acc. | False abst. | Missed abst. |
|---|---|---|---|---|---|---|---|
| bge-reranker-base @ 0.30 (before) | 0.857 | 0.926 | 0.491 | 0.561 | 0.929 | 23 | 2 |
| **bge-reranker-base @ 0.02** | 0.857 | 0.926 | **0.544** | **0.614** | 0.860 | **20** | 2 |
| MiniLM-L6 @ 0.30 | **0.980** | **1.000** | 0.491 | 0.561 | 0.875 | 23 | 2 |

The calibration transferred. On cases it never saw, bge-reranker-base's lower floor
answers 3 more of the 49 answerable questions with no additional missed abstention,
at some cost in citation accuracy. Both floors are now the defaults, per model
(`CALIBRATED_FLOORS` in `documents/rag/local_rerank.py`); an uncalibrated model gets
0.30.

**What the sweep says about the bottleneck.** Moving the floor from 0.30 to 0.001 changed
coverage by at most 6 points on the development set. 14–16 of 30 answerable questions were
declined at **every** floor, and 4–5 of the 7 unanswerable ones were answered at every
floor. The relevance floor is not what limits answers. The extractive answerer is: it
quotes only sentences sharing the question's words, so it declines paraphrases, and it
quotes an on-topic sentence for questions the documents do not answer. That is the
component a generative answerer replaces, and none could be measured here (no provider
credentials).

**The default model is unchanged, and the trade-off is now measured.** MiniLM retrieves
better (R@1 0.980 vs 0.857) and reranks several times faster on CPU; production chat
was 13.8 s per question with bge-reranker-base on the test laptop
([performance](../operations/performance.md)). bge-reranker-base at its calibrated floor
answers 3 more questions out of 57. On CPU-only hardware MiniLM is usually the better
choice: `RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2`.

## System

Latency through the production edge (nginx over TLS → gunicorn → Django) on a
3,000-document corpus is recorded in
[operations/performance.md](../operations/performance.md).

## Unverified

- Any hosted answerer (Claude, Bedrock, OpenAI-compatible): never called for real.
- Any number on institutional documents: there are none in this repository.
- Classification accuracy on institutional document types: the only trained classifier
  predicts RVL-CDIP business classes (test accuracy 0.7185 on 2,000 public pages), which do
  not match institutional types.
