# Performance

What was measured, on what, and what was not. Every number on this page is
**measured** unless it is labelled **estimated**: on the test bed below, 30 September –
5 October 2026, except where a section states its own date and setup. One machine, one
run each: treat the numbers as sizing evidence for this code on modest hardware, not as
capacity guarantees.

## Test bed

| | |
|---|---|
| Host | Laptop, Intel Core i5-8350U (4 cores / 8 threads, 1.7 GHz base), 16 GB RAM, Windows 11, no GPU |
| Containers | Docker Desktop VM limited to 8 CPUs and 7.7 GiB of memory |
| Stack | `docker-compose.prod.yml` with a local test overlay (a disposable database; models from a read-only cache with `HF_HUB_OFFLINE=1`) |
| Edge | nginx with TLS (self-signed certificate) → gunicorn, 3 workers × 2 threads → Django |
| Workers | `worker` (ingest queue), concurrency 2, `--max-tasks-per-child=100`; `worker-index` idle |
| AI configuration | bge-small-en-v1.5 embeddings and bge-reranker-base cross-encoder (both local, CPU), extractive answerer, heuristic scanner, rule-based field extractor, trained text classifier |
| Corpus | 3,000 OCR text documents from the public QS-OCR corpus ([data/datasets.md](../data/datasets.md)) plus 3 end-to-end test uploads: 1.4 KiB of text per document on average, 6,547 chunks (2.18 per document, at most 16), 6,547 vectors of 384 dimensions |
| Database | 86 MB in total: chunk embeddings 27 MB (of which the HNSW index is 13 MB), chunks 21 MB, full-text GIN index 6.5 MB |

## Ingestion

**Accepting documents.** `manage.py ingest_corpus --source qs-ocr` stored 3,000 files, created their rows and
queued a job for each in 200 s: **15.0 documents/s**.

**Processing.** With two worker processes, **1.73 documents/s** in steady state:
2,801 jobs completed in 1,616 s. Five-minute windows ranged from 1.46 to 2.20/s.

| Per job (3,003 jobs) | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Started → completed | 659 ms | 2.5 s | 11.1 s | 34.4 s |

| Stage | n | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| INDEXING (chunk + embed) | 3,003 | 444 ms | 2,047 ms | 7,122 ms | 21,879 ms |
| CLASSIFICATION | 3,003 | 22 ms | 64 ms | 3,481 ms | 11,959 ms |
| EXTRACTION (fields) | 2,410 | 22 ms | 61 ms | 119 ms | 958 ms |
| INGESTION, SCANNING, VALIDATION, TEXT_EXTRACTION, OCR, LAYOUT_ANALYSIS | 3,003 each | 4–6 ms | 16–21 ms | ≤ 51 ms | ≤ 891 ms |

INDEXING is 83% of all stage time and CLASSIFICATION 10%. The corpus is plain
text, so OCR did no work here: **scanned PDFs will be far slower**, and that has not
been measured at scale.

### What the run found

- **Ingestion stopped dead after exactly 200 documents.** That is two worker
  children × `--max-tasks-per-child=100`. Each recycled child loaded the embedding
  model inside Celery's start-up hook. That took longer than the 4-second limit Celery
  allows a child to report itself up, so every replacement was killed, and so was the
  next. Fixed by warming the model in a thread (commit `b1c8282`). There were zero
  kills over the rest of the run.
- **Every recycle costs a model load.** The 22 warm-ups logged took 11–18 s each,
  under load. A task needing the model meanwhile waits on the provider's load lock.
  *Estimated* cost: about 0.15 s per document amortised over 100 documents per
  child. Raise `--max-tasks-per-child` if memory growth allows; that has not been
  measured.
- **A killed worker's tasks were not lost.** Two jobs held by the worker killed
  during the first run's failures finished when the stack next started. Celery's
  `acks_late` left the messages in Redis, which redelivers them after its visibility
  timeout (one hour). A pool *child* killed by the hard time limit or the OOM killer
  is a different case. Celery acknowledges that as failed, and
  `recover_stalled_jobs` reclaims the job. See the comment on
  `CELERY_TASK_ACKS_LATE`.
- **The stack did not survive a host reboot.** The backend's Prometheus directory is
  a tmpfs, and Docker remounts it root-owned on any container restart. The
  unprivileged process could no longer write its metrics and crash-looped. Fixed with
  explicit tmpfs ownership; `check_deployment_config.py` now rejects the pattern.

## Latency through the edge

One client, sequential requests, signed in with a session as a browser is, over TLS
through nginx. A warm-up request preceded each series.

| Request | Caller sees | n | p50 | p95 | max |
|---|---|---|---|---|---|
| Search (hybrid, 10 results) | 3,003 documents (admin) | 30 | 160 ms | 376 ms | 432 ms |
| Search | 429 documents (one department) | 30 | 158 ms | 379 ms | 420 ms |
| Document list | 3,003 | 20 | 49 ms | 98 ms | 112 ms |
| Document list | 429 | 20 | 72 ms | 133 ms | 137 ms |
| Dashboard | 3,003 | 20 | 42 ms | 56 ms | 275 ms |
| Dashboard | 429 | 20 | 68 ms | 132 ms | 147 ms |
| **Chat** | 3,003 | 10 | **13.8 s** | 19.4 s | 19.4 s |

**Chat is slow on this hardware, and the reranker is why.** Per-stage medians were:
retrieval 188 ms, **rerank 13.4 s**, answer 1 ms, grounding 1 ms. Each question
reranks `RAG_RETRIEVAL_DEPTH` = 20 passages of up to 512 tokens with bge-reranker-base
on CPU, which is about 0.65 s per pair. Options, in order of effect:

- Use `RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2`. On the
  evaluation benchmark it reranks several times faster and retrieves better. It
  answers slightly fewer questions; see [evaluation](../evaluation/README.md#calibration).
- Add a GPU.
- Lower `RAG_RETRIEVAL_DEPTH`. Its effect on what answers are built from has been
  measured since, though not its time saving; see
  [chat latency](#chat-latency-on-the-development-stack).

Eight of the ten chat questions abstained. They were generic questions over a corpus
of unrelated business documents, so that is expected. It is not a quality measurement.

### With the recommended pilot configuration (MiniLM), measured 2026-10-06

The same stack, edge, worker settings and measurement script were used, on the same 3,000
QS-OCR documents (6,543 chunks: the earlier corpus less its 3 test uploads). Two things
differed:

- the reranker was `cross-encoder/ms-marco-MiniLM-L6-v2` at its calibrated floor;
- the trained classifier was off, as in the pilot configuration.

| Request | n | p50 | p95 | max |
|---|---|---|---|---|
| Search (hybrid, 10 results) | 30 | 153 ms | 236 ms | 1,039 ms |
| Document list | 20 | 45 ms | 50 ms | 51 ms |
| Dashboard | 20 | 43 ms | 51 ms | 55 ms |
| **Chat** | 10 | **1.57 s** | **3.26 s** | 3.26 s |

**Chat is about nine times faster than with bge-reranker-base** (13.8 s → 1.57 s at the
median). The rerank stage fell from 13.4 s to 1.32 s. Other per-stage medians: retrieval
152 ms, answer 2 ms, grounding 1 ms. This confirms through the edge what the in-process
benchmark predicted. Four of the ten questions abstained (eight with bge-reranker-base).
Those are generic questions over unrelated documents, so this is not a quality comparison.

Processing the 3,000 documents took 1,494 s, about 2.0 documents/s on the same 2 worker
processes. This is not comparable with the 1.73/s above, because the classifier stage
was off.

**Memory** (`docker stats`, same run):

| Service | Memory |
|---|---|
| Backend | 1.2 GiB idle, 1.5–1.7 GiB under load. This is 2.1 GiB with bge-reranker-base |
| Ingest worker | about 1.1 GiB while processing |
| `worker-index`, **idle** | **2.3 GiB** |

The index worker's size comes from its default of `CELERY_INDEX_CONCURRENCY=6`, which
suits a hosted embedder. With the local model, each of its 6 processes loads the model
(about 490 MB each) and then waits for a re-index that rarely comes. **Set
`CELERY_INDEX_CONCURRENCY=1` (or 2) on a pilot server with local models.** That should
leave roughly 0.5–1 GiB for the index worker (estimated from the per-process size, not
measured).

**Concurrent load was attempted and is not reported.** In that run, single-client
latencies were five times the sequential figures above, measured an hour earlier on the
same stack (search 827 ms against 153 ms). That points to other work on the laptop, not
to the system under test, so the concurrent figures from the same run cannot be trusted.
Concurrent load remains **unmeasured**.

## Query plans

`EXPLAIN ANALYZE` on the two retrieval queries exactly as search builds them, for an
administrator (6,547 searchable chunks) and a department member (576):

| Query | Admin | Department member |
|---|---|---|
| Lexical (full text, top 50) | 59.7 ms | 12.1 ms |
| Vector (cosine, top 50) | 60.8 ms | 9.9 ms |

**The vector query does not use the HNSW index.** The authorization filter (current
version, current extraction pass, visible documents) is applied by joins, and Postgres
computes the distance for every authorized chunk and keeps the top 50. Two
consequences, both measured:

- **Results are exact.** Recall is 50 of 50 against an index-free scan, for three
  queries × two users. The well-known pgvector failure is absent: an HNSW scan that
  filters after the fact and returns too few rows for a small department.
- **Cost grows with the chunks a caller may see.** It is about 9 µs per chunk here.
  *Estimated* by linear extrapolation from this one point: about 0.25 s for a
  12,000-document corpus of this kind (about 26,000 chunks), and about 1 s at 100,000
  chunks. Before that scale, measure an HNSW path with pgvector's iterative index
  scans (0.8+) against this exact baseline.

At this corpus size neither half dominates: the two callers' search times are the same.

## Chat latency on the development stack

Measured 17–20 October 2026 on the same laptop, on the development database: 1,730
documents with 7,564 searchable chunks, one of them a PDF of **2.26 million characters**
holding 3,392 of those chunks. The configuration was the pilot one: bge-small embeddings,
the MiniLM cross-encoder, and Gemini answering through the OpenAI-compatible answerer. Other
workloads shared the CPU throughout, so read the absolute numbers as slow-case figures and
the comparisons as the result. The before/after comparisons ran old and new code
alternately on the same questions, and the reranker variants were interleaved likewise.

**Where the time went.** Each stored chat answer records its per-stage timings; 14 of the
49 on the development database do. Of the rest, 32 predate the field and 3 were small talk,
which skips retrieval. Retrieval took
80–180 ms on 12 October. The large PDF was uploaded at 12:18 on the 13th, and from 12:39
retrieval took 1.0–2.3 s that day, and 3.4 and 9.4 s on the 17th. (Three answers on
11 October, before the upload, took 0.4–2.7 s.) Each retrieved candidate was fetched with
its version row, and the version row carries `extracted_text`: the whole document's text.
A question answered from that PDF retrieves up to 100 candidates (50 lexical, 50 vector),
and 58 to 100 of them came from the PDF, each carrying a copy of all 2.26 million
characters: by calculation (**estimated**), **131–226 MB** moved out of Postgres per
question. Reranking took 0.8–5.7 s. Generation (Gemini) took
1.2–36.5 s, which is network and provider time.

**What changed** (branch `chat-latency`):

- Candidates no longer load `extracted_text` or `search_vector`. Both are loaded if
  anything asks for them, so no result can change.
- Once authorization has found something to search, the query is embedded on its own
  thread alongside the lexical query instead of after it.
- The cross-encoder scores each distinct passage once. The same file uploaded twice
  retrieves identical chunks side by side; on the development database, 150 of the 7,564
  searchable chunks repeat another chunk's text.
- Each process remembers the scores the cross-encoder computed, by user, query and
  passage (`RAG_RERANKER_LOCAL_CACHE_SIZE`, 4,096 pairs; `0` turns it off), so a question a
  user asks again of the same worker skips the forward pass. 7 of the 19 questions asked on
  the development stack from 10 to 17 October repeated an earlier one. Never across users:
  a remembered score makes an answer seconds faster, and that difference would tell one
  user what another had asked.
- A chat turn reads the conversation history once, as role and content. Follow-up
  resolution used to read it whole, citations included, on every question, and the
  general-knowledge reply read it again whenever it answered.

**Measured, chat path without persistence** (retrieval, rerank, context, answer,
grounding; the extractive answerer so no provider time is mixed in). Three rounds, five
questions of each kind per round:

| Questions | Code | Retrieval p50 | Rerank p50 | Total p50 | Total p95 |
|---|---|---|---|---|---|
| Answered from the 2.26 M-character PDF | before | 8,769 ms | 8,315 ms | 17.4 s | 21.1 s |
| | after | **803 ms** | 7,269 ms | **8.9 s** | 13.8 s |
| Answered from ordinary documents | before | 1,405 ms | 6,110 ms | 8.1 s | 10.2 s |
| | after | **610 ms** | 5,008 ms | **6.3 s** | 8.5 s |

All ten questions got the same answer before and after: the same abstentions and the same
cited chunks. With the retrieval queries alone, interleaved: lexical 2,256 → 228 ms and
vector 2,554 → 224 ms for questions answered from the large PDF, 961 → 185 ms and
203 → 164 ms for the rest. (The rerank score cache came later and is not in this table;
every question here was asked once per process.)

**A question asked again** by the same user, of the same process, with the score cache
(measured 23 October; five questions, each asked twice; the extractive answerer, as in
the table above): reranking took 3.2–4.7 s the first time and 0 ms the second, and the
whole answer 3.6–5.1 s, then 0.24–0.55 s. All five answers were identical both times. A
generative answerer's own time is not saved, and each gunicorn worker process keeps its
own cache, so a repeat that reaches another worker is reranked again.

**The reranker is now the cost, and it is arithmetic.** Under load, 20 MiniLM pairs took
2.3 s (median over 14 questions), of which tokenizing took 41 ms; the rest is the forward
pass. **Estimated** by counting operations: 20 pairs of about 250 tokens are about
106 GFLOP, which is of the order of a second on this 4-core CPU even when it is idle.
Measured and not adopted (each against 2.3 s for the configuration in use, or 2.5 s in the
ONNX run):

| Tried | Result |
|---|---|
| int8 dynamic quantization (torch) | No faster on this CPU (2.6 s), and it flipped 1 of 280 relevance-floor decisions |
| 8 torch threads instead of 4 | Slower (3.5 s) |
| Batch size 32 instead of 16 | Slower (2.6 s) |
| `max_length` 256 | No faster (3.1 s), and scores changed |
| ONNX Runtime 1.31, the model's published fp32 graph | 17% faster (2.0 s against 2.5 s), with scores within 1.2e-6 of torch's and the same ranking on all 13 questions: not worth a new dependency and a second inference path. The O3-optimized graph was slower (2.7 s); the int8 graph changed the ranking of every question. sentence-transformers' own `[onnx]` extra did not install into this image (pip refused it, reporting conflicting dependencies without naming the package) |
| Fresh planner statistics (`ANALYZE`, inside a rolled-back transaction) | Similar plans and no faster (lexical 63 → 70 ms, vector 126 → 146 ms). Note: `documents_document.archived_at` had no statistics at all on the dev database, so the planner estimated 9 live documents where there were 1,729; most likely the column was added by a migration after the table was last analyzed |
| Driving the exact vector query from the embeddings table | Same nested-loop plan and identical results (183 ms against 188 ms) |
| A warm-up inference at worker start | The first-call penalty was within the noise (316 ms against 246 ms for two pairs; the embedder's first call was faster than its second) |

**Lowering `RAG_RETRIEVAL_DEPTH` would change what answers are built from.** Rerank time
should fall roughly in proportion to the depth (**estimated**: one forward pass per
passage; not measured), but on 30 questions, 20 of which reached the context, the passages
the answer was built from often sat deep in retrieval's fused order:

| Depth | Answers whose context would change | Used passages that sat below the depth |
|---|---|---|
| 8 | 7 of 20 | 17 of 89 |
| 10 | 6 of 20 | 12 of 89 |
| 12 | 3 of 20 | 7 of 89 |
| 15 | 3 of 20 | 4 of 89 |

The default stays at 20. A deployment that needs answers faster than its CPU can rerank
20 passages should add a GPU, or accept that trade knowingly.

## Resource use

Measured during ingestion (`docker stats`):

- **Backend:** 2.1 GiB, three gunicorn workers each holding both models, with
  bge-reranker-base. With MiniLM it is 1.2 GiB idle (see above).
- **Ingest worker:** about 1 GiB at about 5.3 cores. Two children, each with
  torch's default thread count.
- **Index worker:** 2.3 GiB while idle, with its default 6 processes and local
  embeddings. Lower `CELERY_INDEX_CONCURRENCY` (see above).

`EMBEDDING_LOCAL_THREADS` caps torch's threads per process. Setting it to cores ÷
concurrency avoids oversubscription; the effect has not been measured.

## Not measured

- **Concurrent load.** The latency figures come from one sequential client.
  Throughput under many simultaneous users is unknown.
- **OCR-heavy corpora.** Scanned PDFs at scale were not tested; the measured corpus
  is text.
- **Hosted providers.** Bedrock and Anthropic were never called. The OpenAI-compatible
  answerer was called only in development, on Gemini's free tier with a developer's key:
  its generation times are known only from the answers stored then (1.2–36.5 s; see
  [chat latency](#chat-latency-on-the-development-stack)), and none was benchmarked.
- **Restore time at scale.** The restore drill restored a 157 KB dump; see the
  [runbook](runbook.md).
- **Larger corpora.** Nothing beyond 3,003 documents, and no HNSW query path.
