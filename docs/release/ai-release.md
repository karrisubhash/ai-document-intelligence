# AI release readiness

What the AI parts of the system can honestly be said to do at release, what they cannot,
and how the institution should find out the rest. Every number here is from
[evaluation](../evaluation/README.md) or [performance](../operations/performance.md). Both
were measured on one laptop, on **synthetic** documents ([datasets](../data/datasets.md)).

## Two different kinds of quality

| | System engineering quality | AI model quality |
|---|---|---|
| Question | Does the machinery behave as designed? | Are the answers right for *this institution's* documents and questions? |
| Evidence | 1,103 backend tests (2026-10-12); a browser suite through nginx over TLS; two evaluation gates (v1 and v2) that fail CI on any regression; a calibration run on a separate development set | 57 synthetic benchmark questions over 9 synthetic documents, and nothing else |
| Status | **Verified locally** (CI itself has never run on GitHub) | **Unknown for real institutional use** |

The engineering guarantees hold whatever the documents are:

- the permission filter runs before retrieval;
- every answer cites resolvable passages;
- figures are checked against the cited passages;
- an answer that cannot be grounded is turned into a refusal;
- the default answerer only quotes and never generates.

How *often* the system finds the right passage, or declines when it should not, depends on
the documents and the questions. That has only been measured on synthetic data.

## What is safe to claim

- Answers are **extractive**: verbatim quotations from documents the user is allowed to
  read, each with a citation that opens the source page. The default answerer cannot
  invent text.
- The system **declines** when it finds no sufficiently relevant passage, and says why.
- Search and answers **respect department boundaries**. This is enforced in the backend
  and tested, including for search, chat, citations and the audit trail.
- On the synthetic v2 benchmark, the recommended configuration put the right document first
  for **98%** of answerable questions (R@1 0.980) and cited correctly in 87.5% of the
  answers it gave.
- Nothing leaves the host in the recommended configuration: models run locally, and no
  hosted provider is called.

## What is not safe to claim

- **Any accuracy figure on the institution's documents.** None exists.
- **That it answers most questions.** On v2 it answered 26 of 49 answerable questions
  (coverage 0.491). The extractive answerer declines paraphrased questions it has
  retrieved the right passage for. Expect many "I could not find a sufficiently relevant
  passage" replies, especially for questions phrased differently from the documents.
- **That a refusal means the answer is not in the documents.** It often is.
- **That it understands documents in languages other than English.** Both local models
  are English models. Nothing has been tested on another language or on mixed-script
  documents.
- **Scanned-document quality.** OCR (Tesseract) worked in the browser suite on one scanned
  image. It has not been measured on a scanned corpus.
- **Document classification for institutional types.** The only trained classifier
  predicts business-document classes (RVL-CDIP), not institutional ones.
- **Anything about generative answers.** No hosted model has ever been called.

## Reranker decision

Both candidates use the same code path. Switching is one environment variable plus a
`warm_models` run; stored vectors are unaffected, so no reindex is needed.

| | MiniLM-L6 (`cross-encoder/ms-marco-MiniLM-L6-v2`) | bge-reranker-base (`BAAI/bge-reranker-base`, the code's default) |
|---|---|---|
| R@1 on v2 | **0.980** | 0.857 |
| MRR on v2 | **1.000** | 0.926 |
| Answerable questions answered (of 49) | 26 | **29** (at its calibrated floor 0.02) |
| Abstention accuracy | 0.561 | **0.614** |
| Citation accuracy | 0.875 | 0.860 |
| Missed abstentions (answered when it should not) | 2 | 2 |
| Latency, v2 benchmark p50 / p95 (in process) | **644 ms / 3.3 s** | 6.2 s / 16.3 s |
| Latency, chat through the edge, 3,000 documents | **1.57 s p50**, 3.26 s p95 (1.32 s reranking) | 13.8 s p50 (13.4 s of it reranking) |
| Size on disk | **about 90 MB** | about 12 times larger |
| Memory | **backend 1.2 GiB** idle with three workers | backend 2.1 GiB with three workers, each holding both models |
| CPU | several times less per question | about 0.65 s per passage pair on the test laptop |
| Operational complexity | same | same |

**Pilot default: MiniLM-L6 at its calibrated floor (0.30).** On CPU-only hardware, a
13.8-second wait per question would decide the pilot's outcome before answer quality
could. MiniLM is about ten times faster, ranks better, and costs 3 answered questions out
of 57 on synthetic data.

**When to revisit:**

1. Once an institutional evaluation set exists (below), run both rerankers on it at their
   calibrated floors. Switch to bge-reranker-base if it answers materially more questions
   with no increase in missed abstentions, **and** its chat latency on the pilot hardware
   is acceptable to users.
2. When a GPU is available. Measure the latency again; the trade-off changes.
3. If documents are not in English. Both models would need re-evaluating, and a
   multilingual model calibrated with the same procedure.

## Hosted answerer activation

A generative answerer is the component most likely to raise coverage: the extractive
answerer is the measured bottleneck. **It is off by default, and no deployment has called
it.** In development, the Gemini preset of the OpenAI-compatible answerer was run against
Google's free tier on 2026-10-11 with a developer's own key; commits f11dd83, 170084d and
3a34936 record what that found. That was a development check, not an evaluation: no
benchmark was run through a hosted answerer. All of the following are required before
turning it on, on staging first.

**Institutional decisions**

1. Which provider is allowed to receive document text, under what data-processing terms,
   and in which region. Each question sends a third party the following:
   - up to 8 passages, about 8,000 characters;
   - their document titles and page numbers;
   - the question;
   - on follow-ups, words from earlier questions.

   User identities are never sent.
2. Free tiers are excluded. The code itself flags the Gemini and OpenRouter free tiers as
   possibly training on inputs. An unset `RAG_ANSWERER_PROVIDER` with a
   `GEMINI_API_KEY` present selects Gemini.
3. A monthly spend limit, set at the provider. The application has no spend cap. Its
   only bound is the chat throttle (`THROTTLE_CHAT`, 60 questions per user per hour), and
   a question can make up to 4 provider calls (`RAG_MAX_PLAN_STEPS`). A preset with a
   fallback chain - Gemini's has four fallback models - sends a throttled or failing
   call to the next model, so one call can become several requests. A general-knowledge
   reply ([D8](institutional-decisions.md)) is one more call.
4. Whether chat may answer from the model's general knowledge when the documents have
   nothing (`CHAT_GENERAL_KNOWLEDGE`, off by default): decision
   [D8](institutional-decisions.md). It departs from the specification's abstention rule.

**Configuration**

| Setting | Value |
|---|---|
| `RAG_ANSWERER` | `documents.rag.claude.ClaudeAnswerer`, or `documents.rag.openai_compat.OpenAICompatibleAnswerer` |
| Claude via the API | `ANTHROPIC_API_KEY`; `RAG_ANSWERER_BACKEND=api`; `RAG_ANSWERER_MODEL` |
| Claude via Bedrock | `RAG_ANSWERER_BACKEND=bedrock`, `AWS_BEDROCK_REGION`, AWS credentials (environment or instance role), and model access granted in that region |
| OpenAI-compatible | `RAG_ANSWERER_PROVIDER` set **explicitly**, the provider's key variable, `RAG_ANSWERER_MODEL` |
| `RAG_ANSWERER_TIMEOUT`, `RAG_ANSWERER_RETRIES` | **Must be lowered.** Defaults are 60 s and 2 retries, so one call can take 180 s, but nginx stops waiting at 120 s and the user gets a 504 instead of the extractive fallback. A planned question makes up to 4 calls in sequence. Choose `timeout × (retries + 1) × RAG_MAX_PLAN_STEPS` below 120 s, for example 20 s × 1 retry with 2 plan steps. Otherwise raise nginx's `proxy_read_timeout` and gunicorn's timeout for chat |
| Image | the LLM layer is already built into the production overlay (`INSTALL_LLM=true`), pinned by `constraints.txt` |

**Failure modes to know**

- A *misconfigured* answerer does not fall back. Examples are an unknown
  `RAG_ANSWERER_BACKEND`, an unknown provider, or a base URL without a model. The
  constructor refuses, and every chat request returns HTTP 500. The evaluation step below
  catches this before users do.
- A *failing* provider falls back. Timeouts, rate limits, authentication errors and
  outages all return the extractive answer with a note, and increment
  `adi_llm_fallbacks_total`. Alert on that counter.
- The `adi_llm_duration_seconds` histogram's buckets reach 120 s (since e492286; they
  stopped at 10 s before), which matches nginx's 120 s read timeout.
- Token counts are recorded per answerer class (`claude`, `openai-compatible`), not per
  model, so cost per model cannot be derived from metrics.

**Evaluation before enabling** (on staging, with the provider configured for this one
process):

```bash
$DC run --rm --no-deps -e RAG_ANSWERER=… -e … backend sh -c "
  python manage.py seed_evaluation --template /app/evaluation/benchmark-v2.template.json --output /tmp/v2.json --prune &&
  python manage.py evaluate --load /tmp/v2.json --dataset institutional-v2 --dataset-version v2 \
    --as-user <evaluation-user> --baseline /app/evaluation/baseline-v2.json"
```

Copy the exact flags from the CI job (`ai-regression` in `.github/workflows/ci.yml`). This
costs 57 provider calls, more if questions are planned. **Then check
`configuration.degraded` and `metrics.system.answerer_fallbacks` on the stored run.** If
every case fell back to the extractive answerer, the run scores like the extractive
baseline and passes. The command does not print the fallback count. The decision to
enable needs:

- zero fallbacks;
- coverage higher than extractive;
- no more missed abstentions than extractive;
- citation accuracy no worse than extractive.

Then repeat on the institutional evaluation set.

## Real institutional data plan

**Nothing in this repository is institutional data, and none should be fabricated.** This
plan is how the pilot obtains the evidence the synthetic benchmark cannot give.

1. **Choose the pilot corpus** (institutional decision). Use one or two departments and
   documents they already publish to their own members: regulations, policies,
   procedures, circulars. Record for each document its owner, its sensitivity
   classification, and its language. Exclude personal data (student records, salaries,
   disciplinary files) from the pilot unless the data-protection officer approves it.
2. **Ingest on staging, then production.** Upload through the UI or
   `manage.py ingest_corpus` (see [data/datasets.md](../data/datasets.md) for the
   real-document procedure). Record processing failures by error code: OCR quality and
   unsupported layouts appear here first.
3. **Build an institutional evaluation set**, in the same template format as
   `evaluation/benchmark-v2.template.json`:
   - 50–100 questions written by **staff who use the documents**, not by the system's
     developers;
   - for each, the document and page that answers it, or a mark that no document does;
   - a mix: a third phrased as the documents phrase things, a third paraphrased, and a
     sixth near-misses (a related document that does *not* answer);
   - a sixth unanswerable;
   - a second person checks each answer against the document.

   Keep the set inside the institution. It contains institutional content and must not be
   committed to this repository.
4. **Run the evaluation** for the pilot configuration and for each alternative (the other
   reranker; a hosted answerer if one is approved). Store each result as that
   institution's baseline (`evaluate --baseline`).
5. **Human evaluation of live use.** For four weeks, have reviewers each week sample 30
   answered and 20 declined questions from the pilot's chat history. Choose reviewers
   from the departments involved, with consent and under the access the audit log
   records. Record per item:
   - for an answer: correct / partially correct / wrong;
   - whether the citation supports it;
   - for a refusal: whether the answer was in fact in the documents.

   Users' 👍/👎 feedback (`adi_answer_feedback_total`) is a signal, not a measurement:
   people rate fast answers higher.
6. **Decide** against the acceptance criteria below.

### Acceptance criteria (proposed; the institution sets the numbers)

| Criterion | Proposed threshold | Measured how |
|---|---|---|
| Wrong answers | ≤ 2% of answers judged wrong, in human review | step 5 |
| Unsupported citations | ≤ 5% of answers with a citation that does not support them | step 5 |
| Answering when it should decline | ≤ 1 in 10 unanswerable questions answered | step 4 |
| Usefulness | ≥ 50% of answerable questions answered | step 4, coverage |
| Department boundary | zero cross-department disclosures | audit review, and the department-boundary browser test against the pilot |
| Latency | chat p95 within the time users accept (for example 10 s) | `adi_rag_duration_seconds`, pilot hardware |

If usefulness misses but wrong answers and citations pass, the system is safe but not yet
useful. The next step is a hosted answerer under its own approval. If wrong answers or
citations miss, stop and investigate before widening the pilot.
