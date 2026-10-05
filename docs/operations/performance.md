# Performance

What was measured, on what, and what was not. Every number on this page is
**measured** on the test bed below, 30 September – 5 October 2026, unless it is
labelled **estimated**. One machine, one run each: treat the numbers as sizing
evidence for this code on modest hardware, not as capacity guarantees.

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

**Accepting documents.** `ingest_dataset` stored 3,000 files, created their rows and
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
- Lower `RAG_RETRIEVAL_DEPTH`. This has not been measured.

Eight of the ten chat questions abstained. They were generic questions over a corpus
of unrelated business documents, so that is expected. It is not a quality measurement.

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

## Resource use

Measured during ingestion (`docker stats`):

- **Backend:** 2.1 GiB, three gunicorn workers each holding both models.
- **Ingest worker:** about 1 GiB at about 5.3 cores. Two children, each with
  torch's default thread count.

`EMBEDDING_LOCAL_THREADS` caps torch's threads per process. Setting it to cores ÷
concurrency avoids oversubscription; the effect has not been measured.

## Not measured

- **Concurrent load.** The latency figures come from one sequential client.
  Throughput under many simultaneous users is unknown.
- **OCR-heavy corpora.** Scanned PDFs at scale were not tested; the measured corpus
  is text.
- **Hosted providers.** Bedrock, Anthropic and OpenAI-compatible latency: none was
  ever called.
- **Restore time at scale.** The restore drill restored a 157 KB dump; see the
  [runbook](runbook.md).
- **Larger corpora.** Nothing beyond 3,003 documents, and no HNSW query path.
