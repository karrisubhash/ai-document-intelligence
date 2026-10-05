# Architecture

A modular monolith: one Django service with explicit internal boundaries, one Celery worker
pool split across two queues, PostgreSQL (with pgvector) as the only database, Redis as
broker and cache, and nginx at the edge serving the compiled frontend. No component was
split into a separate service, because nothing measured so far justifies the operational
cost of doing so (see [Why one service](#why-one-service)).

```
Browser ── HTTPS ──► nginx ──┬── /            React SPA (static, built into the image)
                             ├── /assets/     hashed bundles, immutable cache
                             └── /api /django-admin /health /ready ──► gunicorn (Django + DRF)
                                                                   │
                     ┌─────────────────────────────────────────────┤
                     │                                             │
               PostgreSQL + pgvector                     Redis (broker, cache, sessions)
   documents, versions, pages, chunks, vectors,                    │
   extraction, conversations, audit, evaluation          Celery workers
                     ▲                                  queue "ingest": the pipeline
                     │                                  queue "index":  re-embedding
                     └──────────────── writes ──────────────────────┘
                                                          │
                                         media (local disk or S3-compatible bucket)
```

## Request paths

**Upload → processing.** `POST /api/documents/` validates the file (size, extension, empty)
before anything is stored, creates a `Document` and an immutable `DocumentVersion`, and
queues a `ProcessingJob` on commit. The response is `202 Accepted`. The worker runs the
pipeline:

```
INGESTION → SCANNING → VALIDATION → TEXT_EXTRACTION → OCR → LAYOUT_ANALYSIS
   → (persist DocumentContent + PageContent + tables + figures, one transaction)
   → CLASSIFICATION → EXTRACTION → INDEXING (chunks + embeddings)
```

Every stage writes a `ProcessingEvent` with status, message, duration and metadata. Stages
whose capability is not configured record `SKIPPED` rather than guessing. Failures are
classified: permanent (corrupt file, malware, timeout) or retryable (database or storage
blip), and only retryable ones are retried, with backoff.

**Search.** `POST /api/search/` computes the caller's authorized document set **first**
(`documents.authorization.visible_documents`), restricts to the current version's current
extraction pass and to non-archived documents, then runs lexical (PostgreSQL `tsvector`)
and vector (pgvector cosine) retrieval over that set and fuses them with reciprocal-rank
fusion.

**Ask.** `POST /api/chat/` resolves a follow-up against the user's own earlier turns, plans
multi-part questions, and for each part runs search → rerank → context construction →
answerer → grounding. An answer that fails grounding becomes an abstention. See
[ai/pipeline.md](../ai/pipeline.md).

## Code map

| Path | Responsibility |
|---|---|
| `backend/accounts/` | Users' profiles, the department registry, session login, expiring API tokens, administration API and its permission rules |
| `backend/documents/authorization.py` | The access rules, as plain functions every path calls |
| `backend/documents/permissions.py` | The DRF policy for documents: 404 to non-readers, 403 to readers who may not manage, audit on both |
| `backend/documents/api/` | Documents API: CRUD, versions, download, content, fields, tables, figures, citation source, archive |
| `backend/documents/services.py` | Version creation (row-locked), reprocess, archive, stalled-job recovery |
| `backend/documents/processing/` | The pipeline, stages, extractors, classifier/extractor/scanner/embedding adapters, model warm-up |
| `backend/documents/search/` | Hybrid retrieval |
| `backend/documents/rag/` | Reranking, context, answerers (extractive, Claude, OpenAI-compatible), grounding, follow-ups |
| `backend/documents/agents/` | Multi-part question planning |
| `backend/documents/chat/` | Chat and conversation endpoints |
| `backend/documents/operations/` | Audit trail, processing monitor, system status, dashboard, health checks |
| `backend/documents/evaluation/` | Governed benchmarks, metrics, regression gate |
| `backend/documents/observability/` | Structured logging with content redaction, Prometheus metrics, telemetry |
| `frontend/` | The product web application ([frontend/README.md](../../frontend/README.md)) |
| `ai/` | Offline training and evaluation of classifiers; imports no Django |
| `infrastructure/` | nginx (edge image, TLS, CSP), Prometheus, Grafana, deployment checks |

## Boundaries that matter

- **Authorization is computed before retrieval.** Similarity search runs only over chunks
  the caller could open directly; there is no "retrieve then filter" path for a leak to hide
  in. Citations therefore can only name readable documents.
- **Documents are data, never instructions.** Retrieved text is delimited as passages in
  the prompt, instruction-like passages are flagged and never quoted as answers, and every
  generated claim is checked against the passages it cites.
- **One live extraction pass per version.** Reprocessing writes a new pass beside the old
  one; `DocumentVersion.current_content` decides which pass search answers from, switched
  in the same transaction that writes it.
- **Providers are replaceable behind one interface each.** Embedding providers, rerankers
  and answerers are named by dotted path in settings, and every one degrades to the bundled
  baseline — visibly — when its backend is unavailable.

## Why one service

The specification sketched FastAPI AI services and a separate gateway. They were not built
because no requirement measured so far needs them:

- The AI components run in-process (local models) or are thin clients of hosted APIs; a
  network hop to a second service would add latency and a failure mode without isolating
  anything the Celery queue split does not already isolate.
- CPU-heavy work (OCR, embedding) already runs in workers that scale independently of the
  API (`ingest` and `index` queues).
- The authorization rules must be identical on every path; one codebase is how that is
  guaranteed.

What would change this: a GPU inference service shared across several applications, or
model memory pressure large enough that API processes should not hold the models. Both are
measurable (see the warm-up memory note in [ai/pipeline.md](../ai/pipeline.md)).
