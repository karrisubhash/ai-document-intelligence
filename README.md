# AI Document Intelligence Platform

An enterprise-oriented system for ingesting, understanding, extracting information from,
searching, and eventually reasoning over heterogeneous business documents — PDFs, Word
documents, scanned pages, images, and plain text.

The capability chain the platform is built around:

```
seeing → understanding → extracting → searching → reasoning → answering → evaluating
```

This repository implements that chain incrementally. The full target design lives in the
project's enterprise specification, *AI Document Intelligence Platform — Enterprise-Level
Technical Documentation* v1.0, which is **not committed to this repository** (`docs/` is
where it belongs if you want it alongside the code). Section references below (§7, §11.2,
§25 …) point into that document. This README describes both **what the specification calls
for** and **what is actually built today**, and never conflates the two.

---

## Status at a glance

| Capability | Spec § | State |
|---|---|---|
| Authentication + roles | 5, 14 | ✅ Built (token auth, profile roles) |
| Document upload, versioning, metadata | 5, 10 | ✅ Built |
| Async processing (queue + worker) | 17 | ✅ Built (Celery + Redis) |
| Validation, ingestion, integrity checks | 5, 10 | ✅ Built |
| PDF/DOCX/TXT parsing + OCR fallback | 11.3 | ✅ Built (pypdf, python-docx, pdf2image, Tesseract) |
| Per-page normalized content store | 15 | ✅ Built (`DocumentContent` / `PageContent`) |
| Classification pipeline stage + contract | 11.1 | ✅ Built — pluggable |
| Trained document classifier + inference adapter | 11.1 | ✅ Built — text baseline, 71.9% test accuracy ([results](ai/classification/README.md#results)) |
| Permission-aware access + audit log | 13, 14 | ✅ Built (department boundary, `AuditLog`) |
| Extraction benchmark harness | 19 | ✅ Built (`benchmark_extraction`) |
| Vision classifier (Kaggle GPU path) | 11.1 | 🟡 Path proven end to end, **untrained** |
| Information extraction (structured fields) | 11.2 | ✅ Built — schema-driven, span-verified |
| Chunking, embeddings, pgvector index | 11.4, 15 | ✅ Built — page-bounded chunks, span-verified |
| Hybrid search + reranking | 12.2 | ✅ Built — RRF fusion + pluggable reranker |
| RAG, citations, abstention, guardrails | 12 | ✅ Built — extractive by default, generative via one setting, same guardrails |
| Testing console (React + Vite) | 26 | ✅ Built — covers the document, search and chat paths, not every endpoint ([frontend/](frontend/)) |
| Metrics, structured logs, AI telemetry | 20 | ✅ Built — `/metrics`, redaction enforced at the formatter |
| Production deploy, HTTPS, backups, CI/CD | 21 | ✅ Built — gunicorn + nginx, verified backups, 8-job pipeline |
| Horizontal scale, queue routing, re-indexing | 22 | ✅ Built — measured, not assumed ([details](#scale-22-nfr-02)) |
| Table detection + table-aware retrieval | 11.3 | ✅ Built — span-verified cells, columns travel with the rows |
| Figures located (not described) | 11.3 | ✅ Built — an abstention that says which page to look at |
| Multi-part question planning | 12 | ✅ Built — 4-part questions went from a flat abstention to 4 of 4 |
| Object storage (S3-compatible) | 15 | ✅ Built — opt-in; the pipeline is storage-agnostic either way |
| Prometheus + Grafana, multiprocess metrics | 20 | ✅ Built — `/metrics` now aggregates across gunicorn workers |
| Governed evaluation dataset + regression gate | 19 | ✅ Built — 13 cases, a committed baseline, and it can fail |
| Product frontend (Next.js), FastAPI AI services | 26 | ⬜ Not started |

`infrastructure/` holds the nginx and deployment configuration; `docs/` and `tests/` are
still placeholders. `frontend/` holds the testing console, not the product frontend §26 describes.

---

## Architecture

Target architecture from the specification (§7):

```
Users → Frontend → API Gateway → Application API
                                   ├── Auth / RBAC
                                   ├── Document Service
                                   ├── Search Service
                                   └── Chat Service
                                          ↓
                        PostgreSQL + Redis + Object Storage
                                          ↓
                              Message Broker / Task Queue
                                          ↓
              OCR · Parser · Classifier · Extraction workers
                                          ↓
                     Chunking → Embeddings → Vector Index
                                          ↓
              Hybrid Retrieval → Reranking → Context Builder
                                          ↓
                    LLM Gateway → Guardrails → Answer + Sources
                                          ↓
                        Evaluation + Observability
```

**Architectural principle (§7.1):** start as a modular monolith with explicit domain
boundaries; extract services only when scale, ownership or failure isolation justifies the
operational cost.

### What runs today

```
Client
  │  Token auth (DRF)
  ▼
Django API  ──────────────────────────────►  PostgreSQL (metadata, jobs, events, audit)
  │  ProcessingJob queued on commit          Local media storage (original artifacts)
  ▼
Redis (broker)
  │
  ▼
Celery worker
  └── run_pipeline()
        INGESTION → SCANNING → VALIDATION → TEXT_EXTRACTION → OCR
                        │
                        ▼
                 LAYOUT_ANALYSIS                 → DocumentTable
                        │                          + DocumentFigure
                        ▼
                 DocumentContent + PageContent   (the internal representation)
                        │
                        ▼
                 CLASSIFICATION                  → ClassificationResult
                        │                        (SKIPPED when unconfigured)
                        ▼
                 EXTRACTION                      → ExtractionResult
                        │                          + ExtractedField
                        ▼
                 INDEXING                        → DocumentChunk
                                                   + DocumentChunkEmbedding
                                                 (chunks always; embeddings
                                                  only when a provider is set)
```

Search runs outside the pipeline, over what INDEXING wrote:

```
POST /api/search/  →  authorization filter FIRST
                          ↓
              ┌───────────┴───────────┐
        lexical (tsvector)      vector (pgvector, cosine)
              └───────────┬───────────┘
                    RRF fusion
                          ↓
                  ranked chunks + citations
```

Every stage writes a `ProcessingEvent` with status, message, duration and structured
metadata, so a document's history is inspectable rather than inferred.

---

## Project structure

```
ai-document-intelligence/
├── backend/                        Django project (modular monolith)
│   ├── config/                     settings, URLs, Celery app, health check
│   ├── accounts/                   UserProfile, roles, /api/auth/me
│   └── documents/
│       ├── models/                 Document, DocumentVersion, DocumentContent,
│       │                           PageContent, ClassificationResult,
│       │                           ExtractionResult, ExtractedField,
│       │                           DocumentChunk, DocumentChunkEmbedding,
│       │                           ProcessingJob, ProcessingEvent, AuditLog
│       ├── api/                    DRF viewset, serializers, routes
│       ├── processing/
│       │   ├── pipeline.py         stage orchestration + content persistence
│       │   ├── stages/             ingestion, validation, text_extraction, ocr,
│       │   │                       classification, field_extraction, chunking
│       │   ├── extractors/         pdf (pypdf), docx (python-docx),
│       │   │                       ocr (tesseract), plain_text
│       │   ├── classification/     DocumentClassifier protocol, service,
│       │   │                       trained text-baseline adapter
│       │   ├── field_extraction/   FieldExtractor protocol, schema packs,
│       │   │                       pure matching core, rule extractor
│       │   ├── chunking/           page-bounded splitter + indexing service
│       │   └── embedding/          EmbeddingProvider protocol + hashing baseline
│       ├── search/                 hybrid retrieval service + /api/search/
│       ├── benchmarks/             extraction benchmark harness
│       ├── authorization.py        access rules as plain, reusable functions
│       ├── permissions.py          DRF policy + audit logging
│       ├── services.py             version creation, checksum, job enqueue
│       └── tasks.py                Celery task + retry classification
├── ai/                             training + evaluation (does NOT import Django)
│   ├── common/                     download + Kaggle helpers
│   └── classification/
│       ├── datasets/               QS-OCR / RVL-CDIP adapters, manifests, integrity
│       ├── preprocessing/          text normalization (travels with the artifact)
│       ├── training/               text trainer, vision trainer, Kaggle submitter
│       ├── evaluation/             pure-Python classification metrics
│       ├── configs/                text_baseline.yaml, vision_baseline.yaml
│       └── artifacts/              model artifacts (git-ignored)
├── frontend/                       React + Vite console for testing the API
│   └── src/
│       ├── api/                    typed client, response shapes, load/poll hooks
│       ├── auth/                   token acquisition and verification
│       ├── components/tabs/        pipeline, content, classification, fields, chunks
│       └── pages/                  documents, document detail, search, chat
├── infrastructure/
│   ├── nginx/                      reverse proxy, TLS termination, rate limits
│   ├── init-letsencrypt.sh         first-issuance TLS bootstrap
│   └── check_deployment_config.py  asserts the prod overlay really overrides
├── .github/workflows/ci.yml        eight-job pipeline (never executed)
├── docs/  tests/                   (placeholders)
├── Dockerfile
└── docker-compose.yml
```

---

## Quick start

### With Docker (recommended)

```bash
cp .env.example .env          # then edit the secrets
docker compose up --build
```

This starts PostgreSQL 16, Redis 7, the Django API (migrations run on boot) and a Celery
worker. The API listens on `http://localhost:${APP_PORT}` (default `8000`).

The image installs `poppler-utils` and `tesseract-ocr`, which the OCR path requires.

```bash
docker compose exec backend python manage.py createsuperuser
curl http://localhost:8000/health/
```

### The testing console

A React + Vite frontend for exercising every endpoint below — upload a document, watch it
move through the pipeline stage by stage, and read what each AI stage produced.

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Or, on a machine without Node, `docker compose --profile frontend up` (off by default).
See [frontend/README.md](frontend/README.md). It is a testing console, not the product
frontend §26 calls for: it is built to show what the backend did, including the stages
that recorded `SKIPPED`, the fields that came back `NOT_FOUND`, and the answers that
abstained.

### Locally without Docker

Requires Python 3.12, PostgreSQL, Redis, plus `poppler` and `tesseract` on `PATH`.

```bash
python -m venv backend/venv
source backend/venv/Scripts/activate      # Windows Git Bash
pip install -r backend/requirements-lock.txt

cp .env.example .env                      # set POSTGRES_HOST=localhost, REDIS_HOST=localhost
cd backend
python manage.py migrate
python manage.py runserver
```

In a second shell:

```bash
cd backend
celery -A config worker --loglevel=INFO
```

---

## Configuration

Environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` | Django core |
| `POSTGRES_DB/USER/PASSWORD/HOST/PORT` | Database connection |
| `REDIS_HOST`, `REDIS_PORT` | Redis |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Task queue |
| `APP_PORT` | Host port for the API |
| `CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed to call the API cross-origin. Empty means same-origin only; under `DJANGO_DEBUG` the Vite dev server (`:5173`) is allowed automatically. Credentials stay off, so the browser never attaches session cookies to a cross-origin call |

Pipeline limits, in `backend/config/settings.py`:

| Setting | Default | Meaning |
|---|---|---|
| `MAX_DOCUMENT_SIZE_BYTES` | 50 MB | Rejected at the VALIDATION stage |
| `MAX_DOCUMENT_PAGES` | 500 | PDF page ceiling |
| `OCR_DPI` | 200 | Render DPI for OCR of PDF pages |
| `DOCUMENT_CLASSIFIER` | `""` | Dotted path to a `DocumentClassifier`; empty means classification is **skipped**, never guessed |
| `DOCUMENT_CLASSIFIER_ARTIFACT` | `ai/.../v0.1.0/model.joblib` | Trained artifact the text classifier loads |
| `DOCUMENT_FIELD_EXTRACTOR` | `""` | Dotted path to a `FieldExtractor`; empty means extraction is **skipped**, never invented |
| `DOCUMENT_FIELD_SCHEMA_PACK` | `""` | Schema pack the rule extractor matches; empty uses the bundled institutional pack |
| `DOCUMENT_EMBEDDING_PROVIDER` | `""` | Dotted path to an `EmbeddingProvider`; empty means chunks are still indexed for **lexical** search, just not embedded |
| `DOCUMENT_SCANNER` | `""` | Dotted path to a `DocumentScanner`; empty means the SCANNING stage records that the file was **not scanned** |
| `THROTTLE_LOGIN` / `_UPLOAD` / `_SEARCH` / `_CHAT` | 10/min, 60/h, 120/h, 60/h | Per-endpoint rate limits |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 1000 / 150 | Chunking; changing either requires re-indexing |
| `SEARCH_CANDIDATES` | 50 | Candidates each retrieval half returns before fusion |
| `DOCUMENT_TABLE_DETECTOR` | bundled layout detector | Dotted path to a `TableDetector`. The one capability that defaults **on**, because it is the only one with nothing to configure — it reads structure out of text already extracted. Empty skips LAYOUT_ANALYSIS' table half |
| `DOCUMENT_FIGURE_DETECTOR` | bundled image detector | Dotted path to a `FigureDetector`. Also on by default, and also nothing to configure: it reads the image dictionary a PDF already carries. It does **not** look at the images |
| `RAG_MAX_PLAN_STEPS` | 4 | How many parts a question may be planned into. `1` disables planning; each step is a full retrieval, so this is a bound, not a budget |

Both are also readable from the environment. Enabling the trained classifier:

```bash
pip install -r backend/requirements-ml.txt      # sklearn + joblib, optional
export DOCUMENT_CLASSIFIER=documents.processing.classification.text_classifier.TextDocumentClassifier
```

Once `DOCUMENT_CLASSIFIER` is set, a missing or unloadable artifact **fails**
the stage rather than skipping it — configuring a classifier is a statement
that classification is expected to happen, and skipping would hide a broken
deployment.

Secrets stay out of source control — `.env`, `media/`, `db.sqlite3`, datasets, model
artifacts and training runs are all git-ignored.

---

## API

Base URL: `http://localhost:8000`. All document endpoints require authentication.
(Spec §16 calls for a `/api/v1` prefix; the current routes are unversioned and will be moved
when the public surface is frozen.)

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/auth/token/` | Obtain a DRF auth token |
| `GET` | `/api/auth/me/` | Current user, role and department |
| `POST` | `/api/documents/` | Upload a document — returns **202 Accepted**, processing queued |
| `GET` | `/api/documents/` | List documents the caller is authorized to see |
| `GET` | `/api/documents/{id}/` | Document metadata and status |
| `PATCH` | `/api/documents/{id}/` | Update title / type |
| `DELETE` | `/api/documents/{id}/` | Delete a document |
| `GET` `POST` | `/api/documents/{id}/versions/` | List versions, or upload a new one |
| `GET` | `/api/documents/{id}/processing/` | Jobs with their full stage-event history |
| `POST` | `/api/documents/{id}/reprocess/` | Re-run the pipeline on the current version |
| `GET` | `/api/documents/{id}/content/` | Page-level extracted text for the current version |
| `GET` | `/api/documents/{id}/classification/` | Every classification verdict on the current version, with its model |
| `GET` | `/api/documents/{id}/extraction/` | Every extraction pass, with one row per declared field |
| `GET` | `/api/documents/{id}/chunks/` | Indexed chunks with page, span and embedding identity (paginated) |
| `GET` | `/api/documents/{id}/download/` | Original file — authenticated and audited |
| `POST` | `/api/search/` | Hybrid search over authorized chunks |
| `POST` | `/api/chat/` | Ask a grounded question; returns answer + citations or an abstention |
| `GET` | `/api/evaluations/` | Evaluation runs, each with the configuration that produced it |
| `GET` | `/api/evaluations/{id}/results/` | Per-case results, for tracing a bad aggregate |
| `GET` | `/api/evaluations/{a}/compare/{b}/` | Regression check between two runs |
| `POST` | `/api/messages/{id}/feedback/` | Rate one answer (online quality signal) |
| `GET` | `/api/chat/conversations/{id}/` | Retrieve a conversation (owner only) |
| `GET` | `/health/` | Liveness + database check |
| `GET` | `/metrics` | Prometheus scrape endpoint. **Ungated by default** — `METRICS_ALLOWED_IPS` is empty unless set, and the allowlist only applies when it is non-empty. In production nginx restricts it to private ranges, which is the gate that actually holds |
| — | `/admin/` | Django admin |



### Example

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/token/ \
  -d "username=alice&password=secret" \
  | python -c "import sys, json; print(json.load(sys.stdin)['token'])")

curl -X POST http://localhost:8000/api/documents/ \
  -H "Authorization: Token $TOKEN" \
  -F "title=Exam Regulations 2024" \
  -F "document_type=REGULATION" \
  -F "file=@regulations.pdf"

curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/documents/1/processing/
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/documents/1/content/
```

---

## Document processing

Upload is **never** synchronous with extraction. The API stores the artifact, records a
`ProcessingJob`, returns `202 Accepted`, and the worker does the long-running work (§10).

### Lifecycle

```
Document:      UPLOADED → QUEUED → PROCESSING → COMPLETED | FAILED
ProcessingJob:            QUEUED → PROCESSING → COMPLETED | FAILED

Stages:  INGESTION → SCANNING → VALIDATION → TEXT_EXTRACTION → OCR
                   → LAYOUT_ANALYSIS → [DocumentContent persisted]
                   → CLASSIFICATION → EXTRACTION → INDEXING
```

- **INGESTION** — sniffs the file type from magic bytes rather than trusting the uploaded
  extension (only `.txt` may fall back to the extension, since text has no signature). A
  DOCX shares its ZIP signature with `.xlsx`, `.pptx` and any `.zip`, so the archive is
  opened and asked whether it actually holds a Word document part.
- **SCANNING** — runs *before* validation, deliberately. See
  [Scanning runs before validation](#scanning-runs-before-validation-and-that-order-is-deliberate).
- **VALIDATION** — enforces supported type, size and page limits, and proves the PDF parses.
- **TEXT_EXTRACTION** — pulls the PDF text layer per page. Pages without one are left
  unclaimed rather than silently blanked.
- **OCR** — claims exactly those pages, rendering one page at a time to keep memory bounded.
  Keeping the two paths separate makes each independently measurable.
- **LAYOUT_ANALYSIS** — last in the content phase, because it reads the finished page text:
  a table on a scanned page is not there to be found until OCR has filled that page in. Finds
  tables and locates figures; see
  [Structure and multi-part questions](#structure-and-multi-part-questions-113-25-phase-12).
- **CLASSIFICATION** — reads the persisted `DocumentContent`, never the original file.
- **EXTRACTION** — likewise; see [Information extraction](#information-extraction-112-fr-10).
- **INDEXING** — chunks and embeds the content; see [Search](#search-114-122-fr-11--fr-13).

### DOCX, and what a page means there

Two things about DOCX need stating, because both are easy to get quietly wrong.

**A DOCX has no page count.** Pagination in Word is a *rendering* result — it depends on the
fonts, page size and printer metrics of whoever opens the file — so there is no page number
to read out of the bytes. What the file does record is where the author asked for a page
break. Those are the boundaries used, and `metadata.pagination` says `explicit_breaks` (or
`none`) so a reader can tell an author-declared boundary from a rendered one. Claiming
anything more precise would be inventing structure the file does not contain.

**A DOCX is a ZIP archive**, and archives from untrusted users can be decompression bombs.
The size checked at VALIDATION is the *compressed* size, which says nothing about what it
expands to, so the uncompressed total and compression ratio are checked before any parsing
happens.

Tables are extracted in document order alongside paragraphs — `python-docx`'s `paragraphs`
skips them entirely, and in institutional documents the tables are often where the content
actually is (fee schedules, course lists, signature blocks). A DOCX whose content is only
embedded images produces no text and fails with `EMPTY_EXTRACTION`: rendering and OCR-ing
embedded images is a capability this pipeline does not claim to have.

### Versioning and evidence

Each upload becomes a new immutable `DocumentVersion` with its own storage path, SHA-256
checksum and size. Reprocessing writes a **new** `DocumentContent` row rather than
overwriting the previous one, so a better extractor can be compared against what the old
pipeline produced. Classification verdicts are stored per run with the model name and
version that produced them.

### Failure handling (§17.1, §18)

`StageError` is **permanent by default** — a corrupt, oversized or unsupported document
fails identically on every attempt, so retrying it only burns worker capacity and writes a
dishonest attempt history. Only `TransientError` (dropped connections, temporarily
unreachable services) is retried, up to 3 retries — four attempts in total, since
Celery's `max_retries` counts retries and not the first run — with exponential backoff. Every failure
carries a structured `ErrorCode`: `UNSUPPORTED_FILE_TYPE`, `FILE_TOO_LARGE`,
`TOO_MANY_PAGES`, `CORRUPT_DOCUMENT`, `OCR_FAILED`, `EMPTY_EXTRACTION`, and others.

Retrying is **idempotent per job**. Every stage creates rather than updates, so the pipeline
deletes whatever a previous attempt at the *same* `ProcessingJob` wrote before it starts
(§10.2). History *across* jobs is untouched — a reprocess creates a new job, so its output
sits beside the old rather than replacing it, which is what keeps two pipeline versions
comparable. `ProcessingEvent` rows from failed attempts are deliberately preserved.

---

## Information extraction (§11.2, FR-10)

The `EXTRACTION` stage reads the persisted `DocumentContent` and matches a per-document-type
schema against it, producing one `ExtractedField` row per declared field.

**Schemas are data, not code.** The spec's worked example is an invoice, but this platform's
document types are institutional (`REGULATION`, `CIRCULAR`, `SYLLABUS`, `POLICY`, `FORM`).
Adding an `INVOICE` type would be fiction about what the platform ingests. Instead the
bundled `institutional-v1.json` covers the real types, and `invoice-v1.json` satisfies §11.2
as a second pack — same mechanism, zero code change.

**A value that cannot be pointed at is not evidence.** The service refuses to persist any
span whose offsets do not re-slice out of the stored `PageContent.text` to the claimed text.
That makes fabrication structurally impossible rather than merely discouraged, and the stored
offsets are what the §12.3 citation model will need.

Five outcomes, all distinguishable in stored data:

| Outcome | How it is recorded |
|---|---|
| Nothing configured | stage `SKIPPED`, no result row |
| No schema for this document type | stage `SKIPPED`, message names the type |
| Field genuinely absent | `ExtractedField` with status `NOT_FOUND` |
| Matched but failed validation | status `INVALID`, with the rule it failed |
| Two different values found | status `CONFLICTED`, value empty, every candidate recorded |

The last one matters most: a silent first-wins pick is exactly the confident wrong answer
this project refuses to produce. The matching core is a pure function over
`(page_number, text)` pairs with no Django import, so the same code can be scored offline
against an evaluation set later.

---

## Search (§11.4, §12.2, FR-11 – FR-13)

### Chunking

Chunks **never span pages**. A chunk that straddles a page boundary cannot answer "which
page did this come from", and page number is half of the §12.3 citation model. Every chunk
stores `page_number`, `character_start` and `character_end`, and the service refuses to
write a chunk whose offsets do not re-slice out of the stored page text — the same
invariant field extraction enforces.

### Embeddings — read this before trusting a result

The bundled `HashingEmbeddingProvider` is **deterministic, offline, and NOT semantic**. It
hashes word and character n-grams, so two texts are close when they share *vocabulary*, not
*meaning* — "car" and "automobile" are orthogonal to it.

It exists because no LLM provider is wired up and no API keys exist, and downloading a
sentence-transformer at runtime is not something a worker should do on its first document.
What it buys is the entire mechanism — chunking, vector storage, the pgvector HNSW index,
fusion with the lexical half, the permission filter — built and measurable against a
baseline that costs nothing and never varies.

`semantic = False` travels with every batch, is stored on every embedding row, and is
returned in every search response. Swapping in a real model is a settings change plus the
re-indexing §11.4 already requires.

### Hybrid retrieval

Lexical and vector search answer different questions: vector finds passages *about* the
query, lexical finds the ones that literally contain `REG/2024/117`. They are fused with
reciprocal-rank fusion over **ranks**, not raw scores — a `SearchRank` value and a cosine
distance are not on a comparable scale, and normalising them against each other would
invent a relationship neither claims.

The lexical query OR-s the whole-query form with each individual term. Each rescues a case
the other loses: Postgres tokenizes `REG/2024/117` as one compound lexeme (so splitting it
finds nothing), while the whole-query form AND-s a natural sentence into oblivion.

**Authorization runs before similarity, always.** §14: *"Similarity alone is never an
authorization mechanism."* Ranking an unauthorized chunk and then dropping it would still
let its content influence the results, and would leak its existence through counts and
scores. Candidate counts for an unauthorized caller are zero, not filtered-after-the-fact.

If no embedding provider is configured, chunks are still written and still **lexically**
searchable — half a search is more useful than none, and the vectors can be backfilled.

---

### Retrieval answers from the live content only

Chunks hang off the version and the extraction pass that produced them, and neither is ever
deleted — that is what makes two pipeline versions comparable. It also meant retrieval could
see a document's entire history. Two filters now bound it to the current version's current
extraction pass, and both were load-bearing:

**Superseded versions.** Measured on a two-version regulation, the answerer replied:

```
Attendance minimum is 75 percent [1] ... Attendance minimum is 60 percent [2]
```

— the **withdrawn** rule quoted beside the one in force, same citation formatting, nothing
to tell a reader which is current. For a regulations platform that is the worst failure
available: not a missing answer, but a confidently cited wrong one.

**Superseded extraction passes.** A reprocess writes a full second set of chunks. One
reprocess took search from 2 hits to 3, of which 2 were unique — the same passage retrieved
twice, spending the context budget on a duplicate and letting one piece of evidence count as
two.

`DocumentVersion.current_content` mirrors `Document.current_version` and is set inside the
same transaction that writes the content, so a half-written pass is never reachable. The
migration backfills it; without that, every chunk already indexed would have silently dropped
out of search while the migration reported success. Verified on the live database: **0 of 46
existing chunks were excluded** by either filter.

History is still stored and still served by the version endpoints. It is simply not what a
question gets answered from.

## RAG (§12, FR-14 – FR-16)

```
question → permission-filtered hybrid retrieval → rerank → context
         → LLM gateway → grounding + citation validation
         → answer + sources, or an abstention
```

### The bundled answerer quotes; it does not generate

The bundled `ExtractiveAnswerer` selects the sentences from retrieved passages that best
answer the question and returns them **verbatim** with their markers. It cannot hallucinate,
because every character it emits was copied from a passage the caller was authorized to
read. It also cannot synthesise across passages, resolve pronouns or rephrase — it is a
baseline, not a destination. `generative: false` is in every response, and swapping in a
real provider is one setting (`RAG_ANSWERER`).

### Generative answers (optional)

```bash
pip install -r backend/requirements-llm.txt

# Either the first-party API...
export ANTHROPIC_API_KEY=sk-ant-...

# ...or Amazon Bedrock, on ordinary AWS credentials and no Anthropic key
export RAG_ANSWERER_BACKEND=bedrock
export AWS_BEDROCK_REGION=us-east-1        # a region with model access granted

export RAG_ANSWERER=documents.rag.claude.ClaudeAnswerer
```

**The two backends are not the same API surface**, and the differences are not optional:
Bedrock model ids carry an `anthropic.` prefix (applied automatically - forgetting it is a
404 that reads like the model does not exist), a region is required rather than defaulted,
and **server-side refusal fallbacks are unsupported there** - sending that parameter to
Bedrock is a 400, so the equivalent is client-side middleware registered on the client. The
answerer selects the right shape per backend rather than sending one and hoping.

The extractive baseline quotes sentences, which reads like a search result rather than a
reply: it cannot rephrase, cannot combine two clauses, and cannot say *"no, because..."*.
[`rag/claude.py`](backend/documents/rag/claude.py) is the other side of that trade, behind
the same `LLMProvider` gateway - swapping it in is one setting, and no caller changes.

**No guardrail moves.** A generative model can invent, so everything built to catch
invention now earns its keep:

- retrieval still runs the **permission filter first**, so the model only ever sees passages
  this caller may read;
- injection-flagged passages are still labelled in the prompt as *data to report on, never
  a directive to follow*;
- every claim must carry a resolvable marker, and `grounding.validate` checks the answer's
  content words actually appear in the passages it cited;
- an answer that fails those checks is **downgraded to an abstention** by the engine, not
  returned with a warning attached.

The model is checked, not trusted — the same contract the extractive answerer had, applied
to a component that can actually get it wrong.

Defaults: **Claude Opus 5**, effort `medium` (grounded answering over a handful of short
passages does not need deep reasoning, and latency is felt directly in a chat box), a 60s
timeout rather than the SDK's 10-minute default, and server-side refusal fallbacks — a
disciplinary circular or a safety incident report is exactly the sort of legitimate content
a classifier can decline on.

**A model outage falls back to the extractive answer rather than returning a 500** (§18
makes that the gateway's problem), and records `fell_back_from` so a quietly degraded answer
never passes as a normal one.

**Neither backend has completed a live call from this repository.** The first-party key
available during development authenticated successfully and was then refused for billing
(`credit balance is too low`), and no AWS credentials were present, so the request shapes
have never been validated by a server. They follow the current SDK and are covered by 26
tests against a stubbed client - hallucination caught by grounding, refusal abstains, outage
degrades to the extractive answer, empty context never reaches the model, and the
Bedrock-unsupported parameter is asserted absent on that path - but **treat the first real
call as the test**.

### Hard-wrapped documents used to get cut off mid-sentence

Newlines are treated as clause boundaries, because institutional documents put one clause
per line far more often than they write flowing prose. But a document wrapped at the right
margin puts newlines *inside* sentences too, and the answer came back truncated:

```
Q: What is the attendance minimum?
A: A student must maintain a minimum of 75 percent attendance in each course to [1]
```

which reads as a broken system rather than a terse one. Continuation lines are now rejoined
before sentence splitting, using the conservative half of the classic hard-wrap heuristic:
**only a line beginning with a lowercase letter continues the one above it**, and table rows
are never joined on either side — their newlines are the row separators, and merging two
rows would produce a row that is not in the table at all.

Under-joining costs a slightly short quote. Over-joining would weld a heading onto a clause
and present the result as one sentence from the document.

### Abstention is a first-class outcome (§12.4)

Five distinct reasons, each recorded rather than collapsed into one silence — because "no
documents matched", "nothing was relevant enough" and "the answer could not be grounded"
are three different facts about the system:

| Reason | Meaning |
|---|---|
| `NO_QUERY` | empty question |
| `NO_DOCUMENTS` | no authorized document matched |
| `NO_RELEVANT_PASSAGES` | retrieval found candidates, none beat the relevance floor |
| `MODEL_ABSTAINED` | passages were relevant but none answered the question |
| `UNGROUNDED` | the answerer produced text that failed validation |

The wording is exactly what §12.4 specifies: *"I couldn't find enough information in the
provided documents."*

### Answers are verified, not trusted

Before anything is returned, `grounding.py` asks three questions of the answer:

1. **Does every marker it cites exist?** Citing `[7]` when six passages were supplied is a
   fabricated reference — the cheapest hallucination to detect, and the most damning.
2. **Did it cite anything at all?** An uncited claim cannot be checked by its reader.
3. **Is it supported by the passages it cited?** Measured as a support ratio against *only
   the cited* passages — an answer must not borrow credibility from evidence it never
   referenced.

A failure downgrades the answer to an abstention. An answer that cannot be grounded is
indistinguishable from an invention.

### Documents are data, never instructions (§13)

Retrieved text is fenced, labelled, and the prompt says plainly that anything inside it
which looks like an order is data to report on, not a directive to follow.

Passages that look like injection attempts are **flagged but still retrieved** — dropping
them would let an attacker hide a document from search. They are excluded from being
*quoted as answers*, though: a live test showed a hostile document's false claim
("attendance minimum is 10 percent") being quoted beside the real answer, which launders
manipulative content into looking authoritative. It stays findable via `/api/search/` and
is reported in `suspected_injections`; it just does not get to be the answer. If every
retrieved passage is hostile, the system abstains.

---

## Malware scanning and rate limiting (§13)

### Scanning runs before validation, and that order is deliberate

Validation answers a *usability* question — "is this a document type we support?" Scanning
answers a *security* one — "is this hostile?" Running validation first meant an executable
renamed `.pdf` was rejected as `UNSUPPORTED_FILE_TYPE` and never scanned, throwing away the
one fact worth escalating: that somebody uploaded a binary. INGESTION bounds the file size
first, so nothing unbounded reaches the scanner.

### The bundled scanner is NOT an antivirus

`HeuristicScanner` has no signature database and will not catch a novel dropper. It catches
the realistic file-upload abuse for a document platform — a document that is secretly an
executable, an Office file carrying a VBA macro, a PDF that runs JavaScript or launches a
program, and the EICAR test string. `is_antivirus: false` is in every result. Point
`DOCUMENT_SCANNER` at ClamAV for real coverage.

Three verdicts, and the middle one matters:

| Verdict | Outcome |
|---|---|
| `CLEAN` | processing continues |
| `SUSPICIOUS` | **processing continues**, findings recorded — a macro is a capability, not proof of intent, and blocking every macro-bearing document would make the platform unusable |
| `INFECTED` | document is **QUARANTINED**, never parsed, never retried |

`QUARANTINED` is deliberately distinct from `FAILED` (§10.1): `FAILED` invites a retry,
`QUARANTINED` is a decision not to touch the file. An unconfigured scanner records
**SKIPPED** — saying the file was not scanned rather than implying a clean bill of health —
and a scanner that is *down* blocks the document rather than letting it through unscanned,
because that is exactly when the control matters.

### Rate limiting

Per endpoint class, not one global number: a document listing is a cheap indexed query, a
chat request runs retrieval, reranking and an answerer.

| Scope | Default | Why |
|---|---|---|
| `login` | 10/min | Credential guessing |
| `upload` | 60/hour | Writes bytes, queues worker time |
| `search` | 120/hour | Two retrievers plus fusion |
| `chat` | 60/hour | The most expensive path in the system |

Login is keyed on **IP *and* username**: keying on IP alone lets a botnet spread a guessing
run across addresses; keying on username alone lets anyone lock a known account out by
failing logins against it. Counters live in **Redis**, not the default per-process cache —
with several API workers each would otherwise keep its own counter and the effective limit
would silently become N times what was configured.

---

## Evaluation (§19, FR-17)

The project's rule is that nothing claims a number without a scored set. This is the
machinery that makes the rule enforceable.

```bash
python manage.py evaluate --load dataset.json --dataset institutional-qa     --as-user alice --label baseline
python manage.py evaluate --dataset institutional-qa --as-user alice     --compare-to 1 --fail-on-regression      # for CI
```

### What is measured

| Layer | Metrics |
|---|---|
| Retrieval | Recall@K, Precision@K, MRR, MAP, nDCG@K |
| Generation | groundedness, coverage, abstention accuracy |
| Citations | citation accuracy, source coverage |
| System | latency mean / p50 / p95 / max |

### The regression gate (§19.2)

```bash
python manage.py seed_evaluation          # ingest the corpus, resolve the benchmark
python manage.py evaluate --load evaluation/benchmark.json \
  --dataset institutional-seed --dataset-version v1 \
  --as-user ci-evaluator \
  --baseline evaluation/baseline.json --fail-on-regression
```

**This gate could not fail, for two separate reasons.** The first was known: no dataset
existed, so the CI step skipped. The second was not, and it would have survived fixing the
first — `--fail-on-regression` only does anything alongside `--compare-to <run id>`, and a
fresh CI database has no previous run. The job would have run the benchmark and passed
whatever the scores were. A gate that cannot fail is worse than no gate: it occupies the
slot, and everyone downstream believes quality is being checked.

So the comparison is now against a **committed baseline file**, and the baseline records the
configuration beside the scores — a baseline set with a semantic embedder is not a baseline
for a lexical one.

**Why the benchmark is generated, not committed.** Cases name `relevant_documents`, and those
are database ids. A committed benchmark with hardcoded ids would score retrieval against rows
a fresh database does not have and report a confident zero. What is governed is
`benchmark.template.json`, which refers to documents by slug (`@fee-structure`);
`seed_evaluation` ingests the corpus **through the real pipeline** — so the score covers
extraction, chunking, table detection and retrieval as configured — and substitutes the ids
it created. Document ids stay stable across re-seeds, because a dataset is immutable once
loaded and a recreated corpus would leave it pointing at rows that no longer exist.

Current baseline, lexical retrieval and the extractive answerer:

| | |
|---|---|
| recall@1, MRR, MAP | 1.0 |
| groundedness | 0.9704 |
| citation accuracy | 0.9444 |
| abstention accuracy | 0.9231 |
| coverage | 0.6923 |
| false abstentions | **1** |
| missed abstentions | 0 |

**The benchmark earned its keep on the first run** by finding that false abstention:
*"What is the deadline for applying for revaluation?"* The clause says "within 15 days of the
publication of results" — `deadline` appears nowhere in it, and no stemmer unifies
`applying` with `application`, so one query term of three matches: 0.33 coverage against a
0.34 floor. Retrieval finds the right document; the answerer will not quote from it.

That case is **kept and documented rather than fixed**, because the fix would be to lower a
threshold until the benchmark passes, which is fitting the system to the exam. It is the
lexical-matching limit this README already describes, and it is exactly what a real semantic
embedding model would resolve — so leaving it in means that improvement will register as
one.

**Latency is excluded from the gate by default.** Found by proving the gate could fail:
inflating one quality metric in the baseline correctly failed the run — and so did four
latency metrics, on an otherwise identical system, because wall-clock timing differs every
run. A gate that fails every time gets disabled, taking the real check with it. Latency
still lands in the run record and on the dashboard; `--gate-latency` opts back in.

### Choices that keep the numbers honest

**Evaluation runs through `rag.ask`, not a parallel path.** An evaluation that reimplements
retrieval measures the reimplementation. It therefore also inherits the permission filter —
a benchmark is scored *as a user*, and one without access scores zero rather than quietly
escalating.

**Abstention is not a wrong answer.** Scoring "I couldn't find enough information" as
incorrect would punish exactly the behaviour §12.4 requires. Abstentions are counted
separately, and the two failure modes are kept apart: `false_abstentions` (refused a
question the corpus answers) and `missed_abstentions` (answered one it cannot). A benchmark
of only answerable questions cannot tell a calibrated system from one that never abstains,
so `expect_abstention` cases are first-class.

**Answer quality is averaged over answered cases only.** Folding abstentions in at 0.0 would
make a cautious system look inaccurate rather than cautious.

**`token_overlap` is named for what it measures.** It is *not* answer correctness — a fluent
paraphrase scores badly and a wrong answer built from the right vocabulary scores well. It
is reported as a weak signal beside groundedness, never as a correctness claim.

**Recall over no relevant items is 0.0, not 1.0.** "Found everything there was to find" is
vacuous when there was nothing, and scoring it perfect inflates every average it lands in.

**Regression checks know which direction is good.** Falling latency is an improvement;
rising `false_abstentions` is a regression even though the number went up. Getting that
backwards would report a quality regression as a win — which makes the check worse than
having none.

**Datasets are immutable.** Editing questions would make last month's score and this month's
describe different exams, so a change is a new version. Every run stores the full
configuration — embedder, reranker, answerer, chunking version, relevance floor — because a
metric without what produced it is a number, not a measurement (§19.2).

---

## Observability (§20, NFR-06)

Structured JSON logs on stdout, Prometheus metrics on `/metrics`, and a correlation id
threading both.

### Document content never reaches the logs (§13)

A convention cannot deliver this — one `logger.info(f"answering {question}")` written in a
hurry undoes it, and the failure is invisible until someone reads the logs. So it is
enforced at the formatter, on every record, whatever the caller passed:

- fields named for content (`question`, `answer`, `text`, `password`, …) are replaced
  **wholesale**;
- any other string over 120 characters is **replaced, not truncated** — the first version
  of this trimmed to a prefix, and a test showed what that actually emits: the opening 120
  characters of a confidential document, which is a leak with extra steps. The length is
  reported, the content is not;
- for `adi.*` loggers the message is a single-token event name, so anything after that token
  is interpolated data and is dropped — closing the careless-f-string case completely;
- numbers pass through whatever they are called, because a count cannot carry a document.

Verified live: a question and a document both containing a confidential marker produced
**zero** occurrences of it across all container logs.

### Correlation ids (§16)

Every response carries `X-Request-ID`; a caller-supplied one is adopted, but sanitised
first — the header lands in JSON lines other tools parse, so a caller must not be able to
smuggle quotes or newlines into them.

### Monitoring stack (§20)

Prometheus and Grafana are part of the production overlay. Grafana is bound to **loopback
only** — a Grafana on the open internet with a default password is a bigger hole than
anything it monitors:

```bash
ssh -L 3000:localhost:3000 <host>     # then http://localhost:3000
```

Prometheus discovers backend replicas by **DNS**, not a static host list, so
`--scale backend=3` is picked up without editing anything. A static target would resolve to
whichever container the resolver felt like and silently monitor a third of the fleet.

### `/metrics` under gunicorn

`prometheus_client` keeps one registry per process, and gunicorn runs several. A scrape was
answered by whichever worker accepted the connection and reported roughly **1/N** of the
traffic. That is not noise — it is wrong in the flattering direction: every error count and
latency read low while nothing appeared broken.

`PROMETHEUS_MULTIPROC_DIR` makes each worker write its samples to a shared directory that the
scrape aggregates across. Two hooks in
[`backend/gunicorn.conf.py`](backend/gunicorn.conf.py) keep that correct over time, and both
are easy to omit:

- **dead workers are reaped** (`child_exit`), or a worker recycled by `--max-requests` leaves
  its counters behind forever and its traffic is counted twice;
- **the directory starts empty** (`on_starting`), or a restarted container serves the
  previous run's samples — including counters for a build no longer deployed.

Proven with real subprocesses rather than mocks, since process boundaries are the entire
point: two processes incrementing by 3 and 4 produce **7**, not 3 or 4.

The Celery workers are a different matter and deliberately so — they are separate
*containers*, and the right answer there is a scrape target per container, which is what
Prometheus already does. Their pipeline telemetry is also on stdout as structured JSON.

### Label cardinality is bounded on purpose

Labels are stage names, statuses, error codes and models — never a document id or a raw
path. `/api/documents/7/` and `/api/documents/8/` are one series (`/api/documents/{pk}/`).
Per-document labels are how instrumentation takes down the backend it reports to. Ids go in
logs, where they are cheap and searchable.

---

## Production deployment (§21)

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The overlay is **not** auto-loaded — it has to be named, so a developer never gets
production behaviour by accident and a deploy never inherits a developer's override. It
swaps `runserver` for gunicorn, puts nginx in front, unpublishes the Postgres and Redis
ports, bakes in the ML layer, and drops the source bind-mount so the image is the artifact.

### The gate

`python manage.py check --deploy` goes from **1 error + 5 warnings** to **zero issues**
with production values set. CI runs it with `--fail-level WARNING`, and a test asserts it,
so "clean except the ones we got used to seeing" cannot creep back.

HSTS is **off by default even in production**. It is close to irreversible — a browser that
has seen the header refuses plain HTTP for the full duration — so it is an explicit decision
by whoever owns the domain, not a framework default.

`SECURE_PROXY_SSL_HEADER` pairs with nginx's `X-Forwarded-Proto`. Without both, Django sees
plain HTTP on the internal hop and `SECURE_SSL_REDIRECT` loops forever.

**nginx does not serve `/media/`.** A `location /media/` alias would hand every uploaded
document to anyone who guesses a path, bypassing every access decision in
`documents/authorization.py`. Originals stay behind the authenticated, audited
`/api/documents/{id}/download/`.

### TLS (§21.1)

```bash
NGINX_SERVER_NAME=docs.example.edu CERTBOT_EMAIL=ops@example.edu \
  STAGING=1 sh infrastructure/init-letsencrypt.sh
```

**The production stack previously could not start at all.** `nginx.conf` hard-requires a
certificate; nothing in the repository ever created one, the certificate directory was empty
and untracked (so a clone did not even have it), and there was no certbot service — the ACME
location block and the `certbot_www` volume were plumbing with nothing behind them.

There is a chicken-and-egg problem here that the bootstrap script exists to resolve: nginx
will not start without a certificate, and Let's Encrypt will not issue one until it can fetch
a challenge over HTTP — which needs nginx running. So the script writes a throwaway
self-signed pair at the exact path nginx expects, starts nginx, requests the real certificate
over the webroot, and reloads. Afterwards the `certbot` service renews on a 12-hour timer and
nginx reloads every 6 hours to pick a renewal up.

**Run it with `STAGING=1` first.** That uses Let's Encrypt's staging CA, which issues an
untrusted certificate and cannot burn the rate limit. This flow has **not been exercised
against a real domain** — there is no public hostname pointing at this checkout — so treat
the first real issuance as the test.

### The production overlay is checked, not trusted

```bash
python infrastructure/check_deployment_config.py
```

Compose **merges** sequences across files: a key re-declared in an overlay is appended to the
base file's list, not substituted for it. This has silently undone a production guarantee
twice in this repository, and neither was visible by reading the overlay:

- `ports: []` left the API published on 8000 — reachable with no TLS, no edge rate limiting,
  and free to assert its own `X-Forwarded-Proto`.
- the `volumes:` list left `./backend:/app/backend` in place, so **production ran the working
  tree instead of the built image** — directly underneath a comment reading *"No source bind
  mount: the image is the artifact."*

`!override` fixes both. The gate asserts the merged **outcome** rather than the tag, by
parsing `docker compose config` — Compose's own merge, not a reimplementation of its rules.
It was verified by reverting the fix and confirming it fails. It runs in CI after the image
build.

### Object storage (§15)

```bash
pip install -r backend/requirements-s3.txt
export AWS_STORAGE_BUCKET_NAME=adi-documents      # switches every FileField
export AWS_S3_ENDPOINT_URL=https://minio.internal # for MinIO or Ceph; omit for AWS
```

Local disk is the default and is genuinely sufficient for one node. It stops being
sufficient the moment there are two: `--scale backend=3` writes an upload to whichever
container answered the request, and the other two — and every worker — get a
`FileNotFoundError` for a document that plainly exists.

**The blocker was not configuration, it was a single line.** `run_pipeline` read
`document_version.file.path`, and `Storage.path()` raises `NotImplementedError` on S3, GCS,
Azure and every other remote backend. So the platform could not be pointed at object storage
*at all*, however it was configured: the first stage raised before any document was looked
at, and §15's "S3-compatible object storage" was unreachable from the code rather than
merely unconfigured.

Every extractor genuinely needs a filesystem path — poppler, tesseract and python-docx are
subprocesses, not Python file objects — so the fix is not to abstract that away.
[`documents/storage.py`](backend/documents/storage.py) materializes the object **once per
job**: on local storage it hands back the real path and copies nothing; on a remote one it
streams the object to a temporary file and deletes it afterwards, including when a stage
raises. A pipeline that fails mid-OCR must not leave a confidential document in `/tmp`.

Tested against a storage that refuses `path()` — which is what S3 is — so the seam is
proven without a bucket, credentials or a network. One of those tests asserts the object is
fetched **once**, not once per stage; it is also what caught the first version of these tests
passing vacuously, because `run_pipeline` re-fetches the job and rebuilt the default storage.

### Backups (§21.3)

```bash
python manage.py backup --include-media --prune
python manage.py restore backups/db-....dump --yes
```

`pg_dump` custom-format archives rather than `dumpdata`: JSON has to round-trip through the
ORM to restore, so it depends on the models still matching — exactly the assumption that
fails when you are restoring *because* a migration went wrong.

**Every dump is verified before it is reported as a success** by reading its own table of
contents back. An unverified backup is a belief, not a backup. The manifest records the
sha256, the table count and the migration it was taken at; restore checks the checksum
before dropping anything, and refuses without an explicit `--yes`.

Restore judges success by **querying the restored database**, not by parsing stderr. The
first version reported a completely successful restore as a failure because of a
`SET transaction_timeout` notice — emitted whenever the `pg_dump` client is newer than the
server. Telling an operator their restore failed, during the incident that made them
restore, is how a bad situation becomes worse.

Proven end to end here: backup → restore into a separate database → 12 documents, 7 users,
46 chunks recovered, live data untouched, and a tampered archive correctly refused.

### CI/CD (§21.2)

`.github/workflows/ci.yml` implements the specified chain as separate jobs, so a failure
names the gate that caught it:

```
lint → tests → security → ai-regression → build → staging → smoke → production
```

Tests run against **real** Postgres-with-pgvector and Redis; a sqlite stand-in would pass
while proving nothing about the HNSW query that actually runs. `makemigrations --check`
catches a model change without a migration, which otherwise passes every test and fails on
deploy. The smoke test asserts an endpoint **refuses** anonymous access — one that only
checks for a 200 cannot tell a working deploy from one with authentication switched off.

**This workflow has never executed.** Its YAML is validated and its commands mirror what the
local stack runs, but GitHub Actions cannot run in this environment. Treat the first real
run as the test. The AI-regression job seeds the corpus and gates on a committed baseline
— see [the regression gate](#the-regression-gate-192).

---

## Scale (§22, NFR-02)

§22 says scaling decisions are driven by **measured workload, queue depth, latency and
cost** — not by technology fashion. So this phase is mostly measurement, and the one
architectural change it makes (splitting the queues) is the one the measurements justified.

### The listing endpoint was O(documents)

`GET /api/documents/` issued **one extra query per document**, because the serializer nests
`current_version` with no join — 14 queries for 12 documents, and a little over a thousand
for a thousand. A `select_related("current_version")` on the list action makes it **2
queries, flat at every corpus size**.

The regression test does not pin a magic number; it measures at two corpus sizes and fails
if the count *grew*. A fixed number would either break on an unrelated change or quietly
accept a slope that starts above it.

### Ingestion and indexing are separate queues

```
ingest  →  OCR, parsing, classification      (CPU-bound, low concurrency)
index   →  chunking, embedding, re-indexing   (IO-bound, high concurrency)
```

NFR-02 calls for independently scalable components, and on one shared queue they are not:
the only way to make a bulk re-index finish sooner would be to add workers that also pick
up ingestion. Proven live — with the worker stopped, depth read `ingest=5, index=0`; an
index-only worker then drained `index` 8→0 while `ingest` stayed at 5, untouched.

`acks_late` with `prefetch_multiplier=1`: a killed worker loses nothing, no worker sits on
a minute-long OCR job another could have started, and queue depth reflects what is actually
waiting rather than what has been quietly reserved.

### Queue depth is read from the broker

The gauge was declared in phase 9 and never set — worse than absent, because a panel that
always reads zero looks like an idle system. It now reads Redis `LLEN` per queue rather
than Celery's `inspect`, which asks the *workers* and therefore reports nothing when every
worker is dead — the moment the number matters most. If the broker is unreachable, or is
not Redis, `collect()` returns `{}` rather than a comfortable zero, and never takes the
scrape down with it.

### Load balancing

nginx resolves the `backend` service through Docker's DNS, so `--scale backend=3` needs no
config edit. `least_conn`, not round-robin: request cost here is wildly uneven — a document
listing is milliseconds and a chat request runs retrieval, reranking and an answerer — so
distributing by connection count tracks real load.

Measured in an isolated project: 3 instances served 9/8/13 of 30 requests, and **12 of 12
requests still succeeded with one instance stopped mid-run**.

Two bugs in the production overlay only appeared under `--scale`, and both had been sitting
there looking correct:

- **`ports: []` does not unpublish a port.** Compose *merges* sequences across files, so the
  empty list appended nothing and the backend stayed published on 8000 — reachable with no
  TLS, no edge rate limiting, and free to assert its own `X-Forwarded-Proto`. Fixed with
  `ports: !override []`.
- **Migrations raced.** Three replicas each ran `migrate` on start; the losers died with
  `column "name" of relation "django_content_type" does not exist`. A schema change is a
  deploy step, so it is now a one-shot `migrate` service that the replicas wait on via
  `service_completed_successfully`.

### Controlled re-indexing (§11.4)

```bash
python manage.py reindex --dry-run
python manage.py reindex
```

Changing the embedding model invalidates every stored vector. Re-indexing is one task per
document on the `index` queue — not one long task for the corpus, which cannot be
parallelised, loses all progress when a worker restarts, and makes queue depth meaningless
because the work is one item deep no matter how much is left.

The task checks for an embedding provider **before** chunking, rather than catching an
exception from it. This is not defensive tidiness: `chunk_content` handles a missing
provider internally and returns a summary instead of raising — correct for first-time
indexing, where chunks with no vector are still lexically searchable, and wrong for a
re-index, because chunking deletes the existing chunks first and the embeddings cascade
away with them. A re-index with no provider configured **destroyed working vectors and
wrote none back**. The regression test asserts the embeddings survive, not merely that the
task reported that it skipped.


### Services were deliberately not extracted

§25 lists "service extraction" under this phase and §22 sets the condition for it:
scaling follows measured workload. Nothing measured here justifies it. The queue split
already gives OCR and embedding independent worker pools on independent machines, which is
the actual requirement in NFR-02; carving the same code into separately deployed services
would add network hops, partial failure and a distributed transaction across the pipeline's
delete-then-create, and buy no scaling property the current shape lacks.

The seam is kept ready rather than cut: every capability sits behind a Protocol with its own
exception taxonomy, so a provider can move out of process without the pipeline noticing.
Extraction becomes justified when one stage's resource profile stops fitting the worker it
shares — a GPU embedding model is the obvious candidate — and that is a measurement, not a
decision to take in advance.

---

## Structure and multi-part questions (§11.3, §25 phase 12)

Three capabilities, each built because something measurable was wrong without it.

### Tables

Flattening a document to text destroys the one thing that makes a fee table answerable:
which number belongs to which label. `85000` on its own is not an answer to "what is the
tuition for CSE".

Tables are detected **in the stored page text**, not by re-parsing the original file, and
that one decision drives the rest. A table found in page text has real offsets into it, so
`page_text[start:end]` returns the table verbatim and every cell is a substring of the page.
**A cell the document does not contain cannot be stored** — the stage verifies every table
on every run and drops any row that fails, so a detector that fabricates is refused at the
boundary rather than trusted not to. A separate PDF table parser would emit cells with no
checkable relationship to the text that every chunk, citation and extracted field is already
grounded in.

It also means one implementation covers a PDF text layer, an OCR pass, a DOCX and a plain
text file, because by then they are all just text.

The first version of the space-aligned detector found **nothing**, because it compared the
left edges of columns and real tables right-align numbers under left-aligned headers —
`Present` starts at column 14 and `38` at column 18. What actually holds, for left, right and
centre alignment alike, is that the *gap* between columns is blank in every row. Counting
those corridors tests the thing that makes a layout a table instead of one alignment
convention out of three.

A header is only recorded when the first row is clearly not data. A table of labels against
labels gets none, because guessing would relabel somebody's data.

### Tables in retrieval — what changed, measured

A 120-row table split across chunks by an ordinary splitter produces 17 chunks, of which
**exactly one contains the header row**. The other 16 are columns of numbers with nothing to
say what they count. Keeping the table as a region gives 11 non-overlapping parts, **every one
carrying its column labels**.

A first attempt at this claimed table-awareness prevents rows being cut in half. Measured, it
does not and never did — the splitter already prefers a line break as a boundary, and mid-row
cuts were zero either way. That test was rewritten to assert the benefit that exists.

The header is **not** spliced into the chunk text. It cannot be: chunk text must equal
`page_text[start:end]`, and a prefix that is not on the page would break every citation. It
travels in metadata, and the answer appends it as a labelled aside:

```
B.Tech CSE  85000  2500  60000 [1] (table columns: Programme | Tuition | Exam Fee | Hostel)
```

Two failures found by asking real questions against a live stack, both fixed:

- "What is the hostel fee for MECH?" returned the right row **and then the header row**
  beside it as a second answer.
- "What is the exam fee?" returned the header row **alone** — the only text in the table
  containing those words. The reader was told an exam-fee column exists and never told what
  it says.

So a header is never quoted on its own. When a question matches the labels rather than one
row, the answer is the table itself — labels and rows, verbatim, bounded by size, because a
120-row table pasted into an answer makes the reader do the retrieval.

### Figures are located, not read

A bar chart with no axis labels survives extraction as nothing at all. Figures are recorded
as a **fact** — "page 4 embeds a 640x480 image" — and never as an interpretation. There is no
`kind` column saying CHART, because nothing here can tell a chart from a photograph and such a
column would be read downstream as a measurement. The image dictionary is inspected, never
decoded; decoding is where a hostile PDF gets to allocate memory.

`description` stays empty until a describer is configured, and that empty string is the useful
part. It lets an abstention say where to look:

```
Q: What does the enrolment chart show for 2024?
   abstained: true
   reason:    no passage answered the question, and the documents retrieved
              contain figures this system cannot read
   note:      'Enrolment Chart' has 1 image(s) on page 1; only text found
              inside them was extracted, not the images themselves
```

The wording is deliberately narrower than "cannot read images": OCR *did* run and *did*
extract any text inside them. What was never interpreted is the image as an image.

The hint is scoped to documents **retrieval already surfaced**, which have passed the
permission filter inside search. It adds no new path to a document a caller cannot see — an
"unreadable figure" hint about a document you are not allowed to know exists is still a
disclosure. Figures on a superseded version are never pointed at either.

### Query planning — built because it was measured

§22 says architecture follows measured workload rather than technology fashion, and a
"planning agent" is exactly the component that gets added because it sounds advanced. So it
was measured first, against a five-document corpus containing every answer:

| Question | Single-shot | Planned |
|---|---|---|
| 2 parts | **2 of 2** | 2 of 2 |
| 3 parts | 1 of 3 | **3 of 3** |
| 4 parts | **0 of 4 — a flat abstention** | **4 of 4** |

The four-part case is the one that justifies the module. The answerer scores a sentence by
what share of the *question's* terms it covers, so every part added raises the bar for every
sentence answering any one of them. Past three parts nothing clears it and the system reports
that it cannot find information it holds.

**Two-part questions are deliberately left unplanned.** They already work, and splitting them
would spend a second retrieval to reach the same answer.

There is no synthesis step. Each part is answered by the ordinary engine — same permission
filter, same reranking, same grounding, same abstention rules — and the composed answer is
those answers with their own citations. Markers are renumbered into one sequence as they are
composed, in a single pass, because step two's `[1]` and step one's `[1]` are different
passages and a marker resolving to the wrong one looks checkable and is not. An unanswered
part is reported, never dropped: two paragraphs in reply to a three-part question otherwise
read as a complete answer.

`RAG_MAX_PLAN_STEPS` (default 4) is a bound, not a budget — each step is a full retrieval.

### Two bugs this phase surfaced elsewhere

Neither was in the new code.

**A NUL byte still killed the pipeline.** Stripping was added to the page rows and the very
next line wrote the raw text into `DocumentVersion.extracted_text`, so a document containing a
NUL processed every stage successfully and died on the last write of the transaction — the
exact failure the stripping existed to prevent, moved down three lines. Found by a test that
fed a NUL through the real pipeline instead of through the helper.

**One common word was enough to answer a question.** Coverage is a ratio and a ratio is blind
to how many terms it is over, so on a two-term question a single generic word scores 50% and
clears the threshold:

```
Q: What is the parking policy?
A: TRANSPORT POLICY          <- a document heading, on the strength of "policy"
```

Requiring two *matched* terms as well as the ratio costs real questions nothing — "attendance
minimum", "hostel curfew" and "B.Tech CSE" all match on two or more — and removes the
coincidence. The rule is skipped when the question itself has only one content term, where one
match is all there is to find. Planning surfaced this: shorter sub-questions let one common
word reach a far higher coverage than it deserves.

---

## Security model

Implemented today:

- **Token authentication**, authenticated-by-default DRF permissions.
- **Department as the tenant boundary.** Read access: owner, same department, or system
  admin. Manage (update / delete / reprocess / new version): owner, department admin of that
  department, or system admin. A non-admin's upload is pinned to their own profile's
  department regardless of what the client sent.
- **An unset department is not a tenant.** `UserProfile.department` defaults to `""`, so
  plain equality once made every freshly registered user a colleague of every other one.
  `shares_department()` returns False for an empty department, and `visible_documents()`
  mirrors `can_view_document()` exactly — a listing that showed more than `retrieve` allows
  would itself be the leak.
- **Access rules are plain functions** in `documents/authorization.py`, not buried in a view
  — so search and RAG can call the identical rules later. Similarity is never an
  authorization mechanism (§14).
- **Media is deliberately not served statically.** Django's `static()` helper serves files
  with no authentication at all; original bytes are reachable only through the
  authenticated, audited `/download/` endpoint.
- **Audit log** for view, create, update, delete, download, reprocess and version upload —
  recording denials as well as approvals.
- **Deleting a document reclaims its bytes.** Django deletes rows, never files, so deletion
  used to remove every trace from the database and leave the uploaded document on disk
  permanently — readable to anyone with filesystem access, invisible to every listing, and
  recorded in the audit log as a completed deletion. A `post_delete` receiver removes the
  file, deferred through `transaction.on_commit`: unlinking inside the transaction would mean
  a rollback restores the row and loses the file, which is the one outcome worse than an
  orphan. A storage failure is logged, never raised — the row is gone either way.
- Content sniffing at ingestion, so a mislabelled file is not handed to the wrong extractor.

Roles currently modelled (`accounts.UserProfile`): `SYSTEM_ADMIN`, `DEPARTMENT_ADMIN`,
`FACULTY`, `STAFF`, `STUDENT`. The specification (§14) describes an organization-scoped
`SUPER_ADMIN` / `ORG_ADMIN` / `MANAGER` / `USER` / `VIEWER` model; the current implementation
uses department-scoped roles, and migrating to organization tenancy is an open item.

Added since this section was first written, and documented in full further down:
**malware scanning** (heuristic, not an antivirus — see
[the bundled scanner](#the-bundled-scanner-is-not-an-antivirus)), **rate limiting** at both
DRF and nginx, and **prompt-injection defenses** (flagged passages are retrieved and
reported but never quoted as an answer).

Still not implemented: **encryption at rest**, and organization-scoped tenancy.

---

## AI workspace (`ai/`)

Training and evaluation live outside Django. **The `ai/` package does not import Django, and
Django does not import it.** The only handoff is a versioned model artifact:

```
ai/  training + evaluation
        │  model artifact (name + version)
        ▼
backend/documents/processing/classification/   inference adapter
        │
        ▼
   Django pipeline → ClassificationResult
```

An adapter implements the `DocumentClassifier` protocol and is named by the
`DOCUMENT_CLASSIFIER` setting. While that setting is empty, the CLASSIFICATION stage records
`SKIPPED` — a fabricated label is worse than no label.

### Datasets

Every adapter emits the same `DatasetSample` and the same manifest CSV
(`sample_id,split,label,input_type,input_path`), so training code never learns where the data
came from. Subsets are selected by hash of `(seed, sample_id)`, not a PRNG over input order,
so the same subset comes back on any platform.

| Source | Modality | Size | Notes |
|---|---|---|---|
| `qs_ocr` | text | 252 MB | Tesseract OCR of all 400k RVL-CDIP pages |
| `rvl_cdip` | image | 38.76 GB | Single tarball; split files fetched separately |

```bash
export PYTHONPATH=.

# Download QS-OCR and build a balanced 500/class subset
python -m ai.classification.datasets.qs_ocr --per-class 500

# Integrity gate — runs BEFORE anything is trained
python -m ai.classification.datasets.integrity \
    ai/classification/data/manifests/qs_ocr --sample-limit 0
```

The integrity check fails loudly on split leakage, duplicates, missing or unreadable files,
unknown labels and degenerate class counts — a silent leak produces a number that looks like
a result and isn't one.

### Training

- **Text baseline** (`configs/text_baseline.yaml`): TF-IDF + logistic regression. Chosen
  because it is the only tier that trains to completion on this hardware and has a directly
  comparable published anchor.
- **Vision baseline** (`configs/vision_baseline.yaml`): ImageNet-pretrained ResNet-50,
  fine-tuned on **Kaggle's free GPU** — this machine has no CUDA device. The submitter
  vendors the shared modules into a single kernel script, pushes, polls to completion, and
  downloads `model.pt`, `metrics.json`, `metadata.json` and `labels.json`.

```bash
export KAGGLE_USERNAME=... KAGGLE_KEY=...            # or ~/.kaggle/kaggle.json

python -m ai.classification.training.submit_kaggle --smoke --dry-run   # inspect, don't push
python -m ai.classification.training.submit_kaggle --smoke             # 1 epoch, 20 img/class
python -m ai.classification.training.submit_kaggle --epochs 5          # real run
```

### Reporting rules

**No accuracy is claimed.** The only artifact present is `v0.1.0-smoke` — 1 epoch over 20
images per class, 12.5% accuracy. That is proof the submit → train → download path works end
to end, not a result.

Any number produced here is **benchmark performance on the public dataset used**, never a
claim about real institutional documents; those require a separate, governed evaluation set.
Subset-trained numbers are also not comparable to published RVL-CDIP figures, which use the
official 320k/40k/40k split. Published anchors for that split — 82.23% text
bag-of-words, 90.24% VGG16, 92.69% DiT-large — are quoted here for orientation only.
They are **not** recorded in any config in this repository, and nothing in the code
compares against them.

---

## Evaluation and benchmarking

The extraction harness compares approaches on a real evaluation set instead of assuming the
initial `pypdf` / `tesseract` wiring was the right technology choice:

```bash
cd backend
python manage.py benchmark_extraction ../eval_set \
    --approach pdf_text --approach ocr \
    --json ../extraction_report.json
```

Expected layout — any `.txt` beside a document with the same stem is treated as ground truth:

```
eval_set/
  born_digital/invoice.pdf
  born_digital/invoice.txt      ← optional ground truth
  scanned/report.pdf
  image/receipt.png
```

It reports pages, characters, wall time, peak child memory, failure rate, and — where ground
truth exists — similarity and word accuracy. Without ground truth the quality columns stay
blank and the command says so; accuracy is not quoted from an unscored set.

Classification metrics (`ai/classification/evaluation/metrics.py`) are implemented in pure
Python with no numpy/sklearn dependency, so the definitions every result is reported against
are readable and runnable anywhere. Macro averages weight each class equally, which is the
point for a 16-class problem whose interesting failures are the small, low-text classes.

---

## Testing

```bash
# Backend: 632 tests across documents, ingestion, classification, field
#          extraction, DOCX, search, RAG, tables, figures, agents,
#          storage, object storage, security, evaluation gate,
#          observability, multiprocess metrics, scale, backup,
#          regressions and accounts
cd backend
python manage.py test

# AI workspace: datasets, metrics, preprocessing, thresholds, Kaggle path
export PYTHONPATH=.
python -m unittest discover -s ai/classification/tests -t .

# Console: type checking and a production build
cd frontend
npm run build
```

Coverage today spans pipeline stages, extractor error mapping, retry classification,
authorization rules, audit logging, API status codes and the classifier boundary. The
end-to-end, load, security and AI-regression suites in §23 arrive with the phases they test.

---

## Technology stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Backend | Django 6.1 + Django REST Framework |
| Database | PostgreSQL 16 |
| Cache / broker | Redis 7 |
| Async jobs | Celery |
| Parsing / OCR | pypdf, pdf2image + poppler, pytesseract + Tesseract, Pillow |
| ML | PyTorch (remote GPU), scikit-learn (text baseline) |
| Testing console | React 19 + Vite 6 + TypeScript |
| Containerization | Docker + Docker Compose |

Also in use: **Nginx** (TLS termination and load balancing, `docker-compose.prod.yml`)
and **GitHub Actions** (`.github/workflows/ci.yml`, written but never executed).

Also in use: **S3-compatible object storage** (opt-in, `AWS_STORAGE_BUCKET_NAME`) and
**Prometheus + Grafana** in the production overlay.

Still planned per specification: FastAPI for AI services, a Next.js product frontend, and
OpenTelemetry tracing. (pgvector is in use; the React console above is a testing tool, not
that product frontend.)

LLM and embedding providers remain model-agnostic by design — provider clients are to be
isolated behind gateway interfaces so models stay replaceable (§26).

---

## Roadmap

All twelve phases of the §25 roadmap have been worked through, but "implemented" is
not the same as "complete", and three items inside them are **not built**:

- **Phase 12 lists multimodal RAG and charts.** Tables, figure *location* and query
  planning are built. Nothing in this system reads an image, so there is no multimodal
  retrieval and no chart understanding.
- **Phase 9 lists traces.** There are correlation ids on every log line and no tracing
  backend, no spans and no OpenTelemetry.
- **Phase 8's benchmark is synthetic.** The regression gate now runs and can fail, over a
  governed 13-case dataset — but the corpus is five documents written for it. A score there
  says the pipeline works, not that it works on your regulations.

What each phase does **not** claim is stated in its own section rather than left to be
discovered.

| Phase | Name | State |
|---|---|---|
| 0 | Architecture & environment | ✅ |
| 1 | Identity — users, auth, roles | ✅ |
| 2 | Document platform — upload, versions, lifecycle | ✅ |
| 3 | Async processing — queue, workers, retries, progress | ✅ |
| 4 | Document AI — parsing, OCR, classification, extraction | ✅ PDF/DOCX/TXT/image parsing, OCR, text classification and field extraction all done; vision classifier remains untrained (tracked under phase 12) |
| 5 | Search — chunking, embeddings, pgvector, hybrid retrieval | ✅ (embedding baseline is lexical, not semantic — see below) |
| 6 | RAG — reranking, context builder, LLM gateway, citations, abstention | ✅ (answerer is extractive, not generative — see below) |
| 7 | AI security — prompt/document injection, permission-aware retrieval | ✅ (heuristic scanner, not an antivirus — see below) |
| 8 | Evaluation — datasets, retrieval + generation metrics, regression | ✅ |
| 9 | Observability — metrics, logs, traces, AI telemetry | ✅ (metrics are per-process — see below) |
| 10 | Production — deployment, HTTPS, backups, CI/CD | ✅ (CI workflow written but never executed — see below) |
| 11 | Scale — load balancing, worker scaling, service extraction | ✅ (horizontal scale is proven; service extraction deliberately not done — see below) |
| 12 | Advanced AI — multimodal RAG, tables, charts, agents | ✅ tables, figures and query planning; figures are located, not read — see below |

Scaling decisions are driven by measured workload, queue depth, latency and cost — not by
technology fashion (§22).

---

## Design principles

Carried through every module in this repository:

- Design around requirements and **measurable outcomes**, not demonstrations that happen to work.
- Separate synchronous APIs from long-running AI workloads.
- Keep tenant and permission boundaries along the whole data path; similarity is not authorization.
- Treat documents and retrieved content as **untrusted input**.
- Keep LLM and embedding providers replaceable behind abstractions.
- Version models, prompts and pipeline configurations; store provenance with every output.
- Prefer simple infrastructure first; scale only when justified.
- Build for failure: retries, idempotency, fallback, auditability.
- **Never fabricate an output** — skip, abstain, or fail honestly instead.
