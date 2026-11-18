# AI Document Intelligence Platform

A self-hosted platform for an institution (a college or university organised into departments).
It lets the institution:

- upload its documents: regulations, policies, syllabi, circulars, forms and scanned pages;
- have each one read, understood and indexed automatically;
- search everything a person is allowed to see;
- **ask questions that are answered with citations to the exact page and passage**, or declined
  when the documents do not say.

The stack:

- a Django + Celery backend;
- a React web application;
- PostgreSQL with pgvector;
- Redis;
- Docker Compose behind nginx.

Every AI component is pluggable and has an offline baseline. That covers OCR, classification,
field extraction, embeddings, reranking and answering. The whole system therefore runs with no
external AI service.

> **New here?** Read sections [1](#1-what-the-platform-does) to [4](#4-repository-map), about
> 20 minutes. Then [run it](#5-getting-started). The rest of this file is a reference: use the
> contents list to find what you need.
>
> Two other places help. [`docs/`](docs/README.md) has a deeper page on each topic.
> [docs/architecture/design-history.md](docs/architecture/design-history.md) records *why*
> things are the way they are, and the defects and measurements behind each decision.

## Contents

1. [What the platform does](#1-what-the-platform-does)
2. [Status at a glance](#2-status-at-a-glance)
3. [Architecture](#3-architecture)
4. [Repository map](#4-repository-map)
5. [Getting started](#5-getting-started)
6. [Core concepts and data model](#6-core-concepts-and-data-model)
7. [Document processing pipeline](#7-document-processing-pipeline)
8. [Search](#8-search)
9. [Asking questions: RAG and chat](#9-asking-questions-rag-and-chat)
10. [Collections](#10-collections)
11. [Security model](#11-security-model)
12. [API](#12-api)
13. [Web application](#13-web-application)
14. [Configuration reference](#14-configuration-reference)
15. [Management commands](#15-management-commands)
16. [Testing](#16-testing)
17. [Evaluation](#17-evaluation)
18. [Observability](#18-observability)
19. [Production deployment](#19-production-deployment)
20. [Operations and troubleshooting](#20-operations-and-troubleshooting)
21. [CI/CD](#21-cicd)
22. [AI workspace](#22-ai-workspace)
23. [Technology stack](#23-technology-stack)
24. [Project history](#24-project-history)
25. [Known limitations and open decisions](#25-known-limitations-and-open-decisions)
26. [Working on this codebase](#26-working-on-this-codebase)
27. [Further reading](#27-further-reading)

---

## 1. What the platform does

### Who uses it

The users are staff, faculty and students, plus department administrators and system
administrators. **The department is the tenant boundary.** A person sees their own department's
documents and their own uploads, and nothing else unless they are a system administrator.

### What a user can do

- **Upload** PDF, DOCX, TXT, PNG and JPEG files of up to 50 MB.
  - Processing runs in the background.
  - The document page shows each stage as it runs.
- **Read what was extracted** from each document:
  - page text, with OCR'd pages flagged;
  - detected tables and located figures;
  - structured fields, such as a circular's number and issue date;
  - the classifier's verdict;
  - every version ever uploaded.
- **Search** every document they may read.
  - Keyword and semantic search are combined.
  - Each hit opens the document at the exact passage.
- **Ask questions** in a chat.
  - Answers cite numbered sources. Each source opens the cited passage, highlighted on its page.
  - Follow-ups ("what about MECH?") and multi-part questions work.
  - When the documents do not answer, the system says so and says why.
  - With a generative model configured, answers stream in as they are written. General-knowledge
    replies can be switched on as well; they are clearly labelled.
- **Group documents into collections**, and scope search or chat to one collection.
- **Administer the platform** (administrators only): accounts, departments, the processing queue
  (including retrying failed runs), the audit log and system health.

### The capability chain

```
seeing  →  understanding  →  extracting  →  searching  →  reasoning  →  answering  →  evaluating
OCR,       classification    fields,        hybrid         follow-ups,   grounded,      benchmarks,
parsing                      tables         retrieval      planning      cited          regression gate
```

### Rules the system does not break

These rules explain most of the design choices you will meet in the code.

1. **Never fabricate.** The system skips, abstains or fails honestly instead:
   - a capability that is not configured records `SKIPPED`;
   - a field that is not in the document is `NOT_FOUND`;
   - an answer that cannot be verified against its sources becomes an abstention.

   A confident wrong output is treated as worse than no output.
2. **Authorization comes before similarity.** Search and chat only ever rank passages the user
   could open directly. There is no "retrieve, then filter" path for a leak to hide in.
3. **Documents are data, never instructions.**
   - Text that reaches a language model is fenced off as data.
   - Instruction-like passages are flagged and never quoted as answers.
   - Every generated claim is checked against the passages it cites.
4. **Measure, don't assume.** Thresholds, architecture and defaults change when a measurement
   says so. The documentation labels every claim as verified or unverified.
5. **Keep the evidence.**
   - Versions are immutable.
   - Reprocessing writes a new extraction pass beside the old one.
   - Every stage writes an event.
   - Every access is audited, including refusals.

### Where it came from

The platform was built against an enterprise specification, *AI Document Intelligence Platform —
Enterprise-Level Technical Documentation v1.0*. The specification is not committed to the
repository. References such as §12 or FR-14 point into it.
[docs/architecture/spec-traceability.md](docs/architecture/spec-traceability.md) maps every
requirement to the code and tests that implement it, and lists where the implementation
deliberately differs from the specification.

---

## 2. Status at a glance

As of 2026-10-11, branch `platform-completion`.

| Area | State |
|---|---|
| **Code** | Complete for a **single-host pilot**. Every functional requirement of the specification is implemented or deliberately adapted ([traceability](docs/architecture/spec-traceability.md)) |
| **Backend tests** | 1,080 test methods in 36 files, run against real PostgreSQL + pgvector. Last recorded results: [docs/development/testing.md](docs/development/testing.md) |
| **Frontend tests** | 55 unit and component tests (Vitest). 8 Playwright browser tests against a live stack: **8/8 through nginx over TLS** on the release candidate (2026-10-06) |
| **AI quality** | Measured on **synthetic** benchmarks only. On the 57-question v2 benchmark, the recommended configuration ranks the right document first for 98% of answerable questions. It answers only about half of them, because the default extractive answerer declines paraphrased questions. **Nothing has been measured on real institutional documents** |
| **Generative answers** | Built: Claude through the Anthropic API or Amazon Bedrock, and any OpenAI-compatible API (Gemini, Groq, OpenRouter, Cerebras, Ollama presets). The Gemini preset has been exercised against the live free tier (2026-10-11). The other providers are contract-tested with stub clients only |
| **CI** | A 10-job GitHub Actions workflow exists, and every job's commands have been run locally. It has **never run on GitHub**, because the repository has no remote |
| **Production** | The production compose stack is verified locally: TLS with a self-signed certificate, the browser suite through nginx, and backup, restore and rollback rehearsed. **Not yet deployed to a real server**. Let's Encrypt issuance is unverified |
| **Go-live blockers** | None known in the code. The rest is external (a GitHub repository, a server and DNS) and institutional decisions; see [§25](#25-known-limitations-and-open-decisions) |

**Repository state**

- Work happens on `platform-completion`. It branched from `master`, which is far behind.
- There is no Git remote.
- `release-review-fixes` holds two unmerged commits: three security fixes from an independent
  review, and release-documentation measurements.
- That branch's migration `0018_auditlog_document_department_db_default` collides with
  `0018_collection` here. Merging it needs `python manage.py makemigrations --merge`.

The full picture of what is verified and what is not is in
[docs/release/RELEASE_READINESS.md](docs/release/RELEASE_READINESS.md).

---

## 3. Architecture

This is a **modular monolith**:

- one Django service with explicit internal boundaries;
- one Celery worker pool split across two queues;
- PostgreSQL (with pgvector) as the only database;
- Redis as broker and cache;
- nginx at the edge, serving the compiled frontend.

### Production topology

```
Browser ── HTTPS ──► nginx (edge image: TLS, CSP, rate limits)
                       ├── /  and /assets/      React SPA, compiled into the image
                       ├── /api/  /django-admin/  /health/  /ready/  ──►  gunicorn (Django + DRF)
                       └── /metrics             404 at the edge; Prometheus scrapes internally
                                                                 │
          ┌──────────────────────────────────────────────────────┼─────────────────────────┐
          ▼                                                      ▼                         ▼
  PostgreSQL 16 + pgvector                              Redis 7                    Media storage
  documents, versions, pages, tables,                   Celery broker, cache,      local volume or an
  fields, chunks + vectors, conversations,              sessions, throttle         S3-compatible bucket
  audit log, evaluation runs                            counters, idempotency keys
          ▲                                                      │
          │                                                      ▼
          └───────────── writes ──────────────  Celery workers
                                                  queue "ingest": the processing pipeline   (worker)
                                                  queue "index":  bulk re-embedding          (worker-index)

  Prometheus scrapes backend:8000/metrics and the workers on :9100.
  Grafana listens on 127.0.0.1:3000 only.
```

In development, `docker compose up` runs the same pieces without nginx:

- `runserver` on `:8000`;
- one worker serving both queues;
- the Vite dev server on `:5173`, which proxies `/api` to the backend.

### The three request paths

**1. Upload → processing.**

```
POST /api/v1/documents/   (multipart: title, document_type, file)
  → uploads.validate_upload        size ≤ 50 MB, allowed extension, not empty — BEFORE storing anything
  → services.create_document_version
                                   Document + immutable DocumentVersion (row-locked numbering, SHA-256)
  → ProcessingJob(QUEUED), enqueued on the "ingest" queue when the transaction commits
  ← 202 Accepted
worker: tasks.process_document_task → processing/pipeline.run_pipeline
  INGESTION → SCANNING → VALIDATION → TEXT_EXTRACTION → OCR → LAYOUT_ANALYSIS
  → persist DocumentContent + PageContent + tables + figures   (one transaction)
  → CLASSIFICATION → EXTRACTION → INDEXING (chunks + embeddings)
  → publish: DocumentVersion.current_content now points at this pass
```

**2. Search.**

```
POST /api/v1/search/ {query}
  → authorization.visible_documents(user)        computed FIRST
  → restrict: current version, live extraction pass, not archived, optional filters
  → lexical (PostgreSQL tsvector)  ‖  vector (pgvector cosine, HNSW)
  → reciprocal-rank fusion → ranked chunks with page and character span
```

**3. Ask.**

```
POST /api/v1/chat/  or  /api/v1/chat/stream/ (Server-Sent Events)
  → follow-up resolution (borrows terms from the user's own earlier questions)
  → planning (a question with 3+ parts is split; each part is answered separately)
  → for each part: search → rerank → relevance floor → context → answerer → grounding
  → answer + citations, or an abstention with a reason
    (or a labelled general-knowledge reply, if that is enabled)
```

### Boundaries that matter

- **One set of access rules.** They live in [`documents/authorization.py`](backend/documents/authorization.py)
  as plain functions. Every path calls them: list, detail, every sub-resource, search, chat,
  citations, the dashboard, audit, jobs and evaluations.
- **One live extraction pass per version.** Reprocessing writes a new pass beside the old one.
  `DocumentVersion.current_content` decides which pass search answers from, and it is switched
  in the same transaction that writes the pass.
- **Replaceable providers.**
  - Embedding providers, rerankers, answerers, scanners, classifiers and field extractors are
    each named by a dotted path in settings.
  - Each one sits behind a protocol.
  - Each degrades *visibly* to the bundled baseline when its backend is unavailable.
- **The `ai/` training workspace imports no Django**, and Django imports nothing from it. The
  only hand-off is a versioned model artifact.

### Why one service

The specification sketched separate FastAPI AI services and an API gateway. They were not built,
because nothing measured needs them:

- the AI components run in-process or are thin clients of hosted APIs;
- CPU-heavy work already runs in workers that scale independently (the `ingest` and `index`
  queues);
- one codebase is how the authorization rules are guaranteed to be identical on every path.

What would change this decision is a shared GPU inference service, or enough model-memory
pressure that API processes should not hold the models. Details are in
[docs/architecture/overview.md](docs/architecture/overview.md#why-one-service).

---

## 4. Repository map

```
ai-document-intelligence/
├── backend/                          Django project: the API and the pipeline (one service)
│   ├── config/                       settings.py (ALL configuration), urls.py, celery.py, health.py, wsgi.py
│   ├── gunicorn.conf.py              multiprocess-metrics and model warm-up hooks
│   ├── requirements.txt              base dependencies
│   ├── requirements-{ml,local,llm,s3,otel}.txt   optional layers, chosen by build args
│   ├── constraints.txt               exact pins for every install (image and CI)
│   ├── accounts/                     Department registry, UserProfile + roles, session and token
│   │                                 auth, administration API (users, departments)
│   └── documents/
│       ├── models/                   every domain model, one file per area
│       ├── api/                      documents REST API (viewset, serializers, discovery filters)
│       ├── processing/               the pipeline
│       │   ├── pipeline.py           stage orchestration, persistence, publishing the live pass
│       │   ├── stages/               one module per stage
│       │   ├── extractors/           pdf (pypdf), docx (python-docx), ocr (Tesseract), plain text
│       │   ├── scanning/             scanner protocol + heuristic scanner
│       │   ├── tables/  figures/     layout analysis
│       │   ├── classification/       classifier protocol + trained text-classifier adapter
│       │   ├── field_extraction/     schema packs (JSON) + rule-based extractor
│       │   ├── chunking/             page-bounded splitter, indexing service
│       │   ├── embedding/            hashing baseline, local sentence-transformers, Bedrock Titan
│       │   ├── errors.py             ErrorCode, StageError, TransientError
│       │   └── warmup.py, model_cache.py   offline-first model loading
│       ├── search/                   hybrid retrieval + /search/
│       ├── rag/                      engine, reranking, context, answerers, grounding,
│       │                             follow-ups, general-knowledge replies, streaming
│       ├── agents/                   multi-part question planner and runner
│       ├── chat/                     chat, streaming and conversation endpoints
│       ├── collections/              collection endpoints
│       ├── evaluation/               benchmark loader, metrics, runner, regression comparison
│       ├── evaluation_api/           read-only evaluation API + answer feedback
│       ├── operations/               audit log, processing monitor, system status, dashboard, health
│       ├── observability/            JSON logging with redaction, Prometheus metrics, tracing
│       ├── benchmarks/               extraction benchmark harness
│       ├── management/commands/      backup, restore, reindex, evaluate, seed_*, …  (§15)
│       ├── authorization.py          THE access rules, as plain functions
│       ├── permissions.py            DRF policy: 404 to non-readers, 403 to non-managers, audit both
│       ├── services.py               versions, enqueue, reprocess, archive, retry, stalled-job recovery
│       ├── uploads.py                upload admission, before anything is stored
│       ├── idempotency.py            Idempotency-Key handling for uploads
│       ├── storage.py                storage-agnostic local copy for the pipeline (S3 support)
│       ├── tasks.py, tasks_reindex.py    Celery tasks and retry policy
│       ├── audit.py, throttling.py, signals.py
│       └── tests*.py                 the backend test suite, one file per capability
├── frontend/                         React 19 + Vite + TypeScript web application
│   ├── src/api/                      typed client (CSRF, upload progress, SSE chat stream), hooks
│   ├── src/auth/                     session state and capability checks
│   ├── src/components/               layout, UI primitives, upload dialog, chat/ and document/ panels
│   ├── src/lib/                      formatting, labels, highlighting, safe Markdown renderer
│   ├── src/pages/                    one file per screen; admin/ for administration
│   └── e2e/                          Playwright specs (they need a live stack)
├── ai/                               offline classifier training and evaluation; imports no Django
│   ├── common/                       download and Kaggle API helpers
│   └── classification/               datasets, preprocessing, training, evaluation, configs, tests
├── evaluation/                       synthetic corpus, governed benchmark templates, committed baselines
├── infrastructure/
│   ├── nginx/                        edge image: builds the SPA; TLS, CSP, rate limits, proxy
│   ├── prometheus/                   scrape configuration + 11 alert rules
│   ├── grafana/                      provisioned datasource + "AI Document Intelligence" dashboard
│   ├── init-letsencrypt.sh           obtains the first TLS certificate
│   └── check_deployment_config.py    asserts the merged production compose is safe
├── docs/                             the working reference (index: docs/README.md)
├── .github/workflows/ci.yml          10-job CI pipeline (not yet run on GitHub)
├── Dockerfile                        backend/worker image (python:3.12-slim, runs as uid 10001)
├── docker-compose.yml                development stack
├── docker-compose.prod.yml           production overlay (never loaded automatically)
├── .env.example                      every environment variable, with comments
└── pyproject.toml                    ruff configuration
```

The empty, untracked directories `tests/`, `infrastructure;W/` and `infrastructure/deployment/`
are leftovers. Ignore them; the real tests are listed in [§16](#16-testing).
Git-ignored local data you may see includes:

- `backend/media/`, `backend/db.sqlite3` (unused; the database is PostgreSQL) and `backups/`;
- `ai/classification/{data,artifacts,runs}/`;
- the resolved `evaluation/benchmark*.json` files.

### Where to start reading

| To understand… | Start with |
|---|---|
| Who may see what | [`documents/authorization.py`](backend/documents/authorization.py), then [`documents/permissions.py`](backend/documents/permissions.py) and [`accounts/permissions.py`](backend/accounts/permissions.py) |
| What happens after an upload | `DocumentViewSet.create` in [`documents/api/views.py`](backend/documents/api/views.py) → [`services.py`](backend/documents/services.py) → [`tasks.py`](backend/documents/tasks.py) → [`processing/pipeline.py`](backend/documents/processing/pipeline.py) |
| How a question is answered | [`chat/views.py`](backend/documents/chat/views.py) → [`agents/runner.py`](backend/documents/agents/runner.py) → [`rag/engine.py`](backend/documents/rag/engine.py) (`ask`) → [`search/service.py`](backend/documents/search/service.py) → [`rag/grounding.py`](backend/documents/rag/grounding.py) |
| Every setting and its default | [`config/settings.py`](backend/config/settings.py), the single source of truth, heavily commented; also [§14](#14-configuration-reference) |
| How the UI talks to the API | [`frontend/src/api/client.ts`](frontend/src/api/client.ts) |
| The production topology | [`docker-compose.prod.yml`](docker-compose.prod.yml) and [`infrastructure/nginx/nginx.conf`](infrastructure/nginx/nginx.conf) |

---

## 5. Getting started

### Prerequisites

- **Docker** with Compose v2. The backend, worker, PostgreSQL and Redis run in containers. The
  image carries Tesseract and poppler for OCR.
- **Node 22**, for the frontend dev server. Alternatively, run it in a container (see below).
- **Python 3.12**, only if you want to run `ruff` or the AI workspace on the host, or run without
  Docker.

### First run

```bash
cp .env.example .env
# Edit .env: set DJANGO_SECRET_KEY and POSTGRES_PASSWORD; keep APP_PORT=8000.
docker compose up --build        # db, redis, backend (migrates, then runserver on :8000), worker
docker compose exec backend python manage.py createsuperuser

cd frontend
npm install
npm run dev                      # http://localhost:5173, proxied to the API on :8000
```

To run the frontend in a container instead of on the host: `docker compose --profile frontend up`.

Then:

1. Sign in at <http://localhost:5173> as the superuser. A superuser is a `SYSTEM_ADMIN`.
2. Go to **Administration → Departments** and create the departments. Departments are a
   registry, so a typo cannot create a tenant.
3. Go to **Administration → Users** and create accounts in those departments.
4. Upload a document and watch its processing timeline. The synthetic documents in
   [`evaluation/corpus/`](evaluation/corpus/) are good test material.
5. Search, and ask a question such as *"What is the attendance minimum?"*

Health checks: `curl http://localhost:8000/health/` and `curl http://localhost:8000/ready/`.

> **If chat answers carry a note that the generative answerer fell back:** your `.env` names a
> hosted answerer (for example the Gemini block in `.env.example`) without a key, or the image was
> built without `INSTALL_LLM=true`. Answers are then extractive, which is the safe default. Either
> supply the key and rebuild, or remove `RAG_ANSWERER` from `.env`.

### What is on by default

With nothing configured, the system runs fully offline:

| Capability | Default |
|---|---|
| Text extraction, OCR, DOCX, table detection, figure location | **On** |
| Malware scanning, classification, field extraction | **Off.** The stage records `SKIPPED` |
| Search | **Lexical only.** Chunks are indexed but not embedded until an embedding provider is set |
| Reranking | Lexical reranker |
| Answering | **Extractive.** Quotes sentences verbatim and cannot hallucinate. Nothing is generated or streamed |

### Turning on the optional AI layers

Heavier dependencies are opt-in **image build arguments**. Set them in `.env` or in the shell:

| Build argument | Installs | Enables |
|---|---|---|
| `INSTALL_ML=true` | scikit-learn, joblib | the trained text classifier |
| `INSTALL_LOCAL=true` | CPU PyTorch, sentence-transformers | local semantic embeddings and cross-encoder reranking |
| `INSTALL_LLM=true` | anthropic, boto3, openai | Claude (Anthropic API or Bedrock) and OpenAI-compatible answerers; Bedrock embeddings and reranking |
| `INSTALL_S3=true` | django-storages, boto3 | S3-compatible object storage (passed only by the production overlay) |
| `INSTALL_OTEL=true` | OpenTelemetry SDK and instrumentation | distributed tracing (passed only by the production overlay) |

```bash
INSTALL_LOCAL=true INSTALL_ML=true docker compose up --build
```

Then select the capabilities in `.env`:

```bash
# Semantic search and cross-encoder reranking (local CPU models)
DOCUMENT_EMBEDDING_PROVIDER=documents.processing.embedding.local.LocalEmbeddingProvider
RAG_RERANKER=documents.rag.local_rerank.LocalCrossEncoderReranker
RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2   # recommended on CPU (see §17)

# Pipeline capabilities
DOCUMENT_SCANNER=documents.processing.scanning.heuristic.HeuristicScanner
DOCUMENT_FIELD_EXTRACTOR=documents.processing.field_extraction.rule_extractor.RuleFieldExtractor
DOCUMENT_CLASSIFIER=documents.processing.classification.text_classifier.TextDocumentClassifier  # needs INSTALL_ML
```

After changing the embedding provider, run these once:

```bash
docker compose exec backend python manage.py warm_models   # download the model weights into the hf_cache volume
docker compose exec backend python manage.py reindex       # re-embed existing documents
```

**Generative, streamed chat (optional).** Build with `INSTALL_LLM=true`, then choose one provider:

```bash
# Gemini Flash (free tier available) through the OpenAI-compatible answerer
RAG_ANSWERER=documents.rag.openai_compat.OpenAICompatibleAnswerer
RAG_ANSWERER_PROVIDER=gemini            # or groq, openrouter, cerebras, ollama
GEMINI_API_KEY=...                      # from https://aistudio.google.com/apikey
CHAT_GENERAL_KNOWLEDGE=True             # optional: labelled, uncited replies when the documents have nothing

# …or Claude
RAG_ANSWERER=documents.rag.claude.ClaudeAnswerer
ANTHROPIC_API_KEY=...                   # or RAG_ANSWERER_BACKEND=bedrock + AWS_BEDROCK_REGION
```

> **Data protection.** With a hosted answerer, retrieved document text is sent to that provider.
> Free tiers (Gemini, OpenRouter) may use what is sent to improve their products. Do not use them
> with confidential documents. Whether any hosted provider may receive document text is an
> institutional decision ([§25](#25-known-limitations-and-open-decisions)).

### Recommended pilot configuration

This is the configuration verified end to end on the release candidate:

- local `bge-small-en-v1.5` embeddings;
- the **MiniLM** cross-encoder, which is about 10× faster than `bge-reranker-base` on CPU and
  ranked better on the benchmark;
- the heuristic scanner;
- rule-based field extraction;
- the extractive answerer;
- `HF_HUB_OFFLINE=1` after `warm_models`, so nothing contacts the model hub at runtime.

The trained classifier is optional. Its classes are business-document types from RVL-CDIP, not
institutional ones (see [§22](#22-ai-workspace)).

### Running without Docker

You need Python 3.12, PostgreSQL with pgvector, and Redis, plus `poppler` and `tesseract` on
`PATH`.

```bash
python -m venv backend/venv
source backend/venv/Scripts/activate        # Windows Git Bash; bin/activate elsewhere
pip install -r backend/requirements.txt -c backend/constraints.txt
cp .env.example .env    # set POSTGRES_HOST=localhost, REDIS_HOST=localhost,
                        # CELERY_BROKER_URL=redis://localhost:6379/0, CELERY_RESULT_BACKEND=redis://localhost:6379/1
cd backend
python manage.py migrate
python manage.py runserver
# second shell:
celery -A config worker --loglevel=INFO --queues=ingest,index
```

### Everyday commands

```bash
docker compose run --rm -T -e LOG_LEVEL=ERROR backend python manage.py test --noinput   # backend suite
docker compose exec backend python manage.py makemigrations                            # after a model change
backend/venv/Scripts/ruff.exe check backend ai                                          # lint (ruff 0.16.8)
backend/venv/Scripts/ruff.exe format backend ai                                         # format
cd frontend && npm run typecheck && npm test && npm run build                           # frontend checks
```

### Windows notes

This project was developed on Windows with Git Bash and Docker Desktop.

- **Git Bash rewrites container paths.** It turns `/app/x` into `C:/Program Files/Git/app/x`.
  Prefix such `docker compose run …` commands with `MSYS_NO_PATHCONV=1`.
- **Shell scripts must stay LF.** `.gitattributes` enforces `*.sh eol=lf`. A CRLF entrypoint
  makes the nginx image exit with code 127.
- **Don't run the Django test runner with `--parallel`.** One test error is not picklable, so the
  parallel runner hides the real failure behind a pickling traceback.
- **A stopped `npx vite` can keep its port.** Check for a lingering node process if `:5173` is
  busy.

---

## 6. Core concepts and data model

### Glossary

| Term | Meaning |
|---|---|
| **Department** | The tenant boundary. A registry of names (`accounts.Department`). Documents, profiles, collections and conversations store the department name |
| **Role** | One of `SYSTEM_ADMIN`, `DEPARTMENT_ADMIN`, `FACULTY`, `STAFF`, `STUDENT` (`accounts.UserProfile`). New accounts default to `STUDENT`; a Django superuser is given `SYSTEM_ADMIN` |
| **Capability** | A flag derived from the role, such as `administer_users`, returned by `/api/v1/auth/me/`. The UI uses capabilities to decide what to show. The API enforces the same rules independently |
| **Document** | The logical document: title, type, department, owner, status, current version, and optionally an archive date |
| **Version** | An immutable uploaded file with a SHA-256 checksum. Uploading again creates a new version |
| **Processing job / event** | A job is one pipeline run over one version. An event records one stage's outcome within that run |
| **Extraction pass** | A `DocumentContent` row: the per-page text produced by one run. Reprocessing creates another pass. `DocumentVersion.current_content` is the live one |
| **Chunk** | A span of one page's text: the unit of retrieval and citation. A chunk never crosses a page boundary |
| **Embedding** | One model's 384-dimensional vector for one chunk. Its model name and version are stored with it |
| **Collection** | A named set of documents within one department. It narrows search and chat; it never grants access |
| **Citation** | Document, page, chunk, character span and quoted text. It resolves back to exact words |
| **Abstention** | A deliberate *"I couldn't find enough information in the provided documents."*, with a recorded reason |
| **Grounding** | The checks every answer must pass against the passages it cites |
| **Provider** | A pluggable implementation (embedder, reranker, answerer, scanner, …) selected by a dotted path in settings |

### Models

**`accounts` app**

| Model | Purpose |
|---|---|
| `Department` | Registry of tenant names: `name` (unique), `code`, `description`, `is_active`. There is no delete; departments are deactivated |
| `UserProfile` | One-to-one with Django's `User`. Holds `role` and `department` |

**`documents` app** (`backend/documents/models/`)

| Model | Purpose and key fields |
|---|---|
| `Document` | `title`, `document_type` (`GENERAL`, `REGULATION`, `POLICY`, `SYLLABUS`, `CIRCULAR`, `FORM`, `OTHER`), `department`, `owner`, `current_version`, `status`, `archived_at` / `archived_by` |
| `DocumentVersion` | `version_number`, `file`, `file_name`, `file_size`, `mime_type`, `checksum`, `current_content` (the live pass), `uploaded_by` |
| `ProcessingJob` | One run: `status`, `current_stage`, `attempt`, `error_code`, `error_message`, timestamps |
| `ProcessingEvent` | One stage attempt: `stage`, `status` (`STARTED`, `SUCCEEDED`, `SKIPPED`, `FAILED`), `message`, `metadata` (duration, error code, findings) |
| `DocumentContent` / `PageContent` | One extraction pass, and its pages. Each page has `text`, `extraction_method` (`PDF_TEXT`, `OCR`, `PLAIN_TEXT`, `DOCX_TEXT`) and `character_count` |
| `DocumentTable` / `DocumentFigure` | Detected tables (span, rows, header) and located figures (page, size; `description` is empty unless a describer is configured) |
| `ClassificationResult` | `model_name`, `model_version`, `predicted_label`, `confidence` |
| `ExtractionResult` / `ExtractedField` | One field-extraction pass, and one row per declared field. A field has a `status` (`FOUND`, `NOT_FOUND`, `INVALID`, `CONFLICTED`), a value and its source span |
| `DocumentChunk` / `DocumentChunkEmbedding` | Chunk: page, span, text and a `search_vector` (GIN index). Embedding: model identity, `semantic` flag and a `VectorField(384)` with an HNSW cosine index |
| `Collection` | `name` (unique per department), `description`, `department`, `created_by`, a many-to-many `documents` |
| `Conversation` / `Message` | A user's chat thread. Each message holds role, content, abstention and reason, citations, support ratio, model, timings, and `metadata` (`source`, `plan`, `followup`, …) |
| `Feedback` | A user's rating of one answer: `HELPFUL`, `NOT_HELPFUL` or `INCORRECT`, plus a comment |
| `AuditLog` | `user`, `action`, `result` (`ALLOWED` / `DENIED`), `document`, and a snapshot of the document id and department that survives deletion. Also `metadata` |
| `EvaluationDataset` / `EvaluationCase` / `EvaluationRun` / `EvaluationCaseResult` | Governed benchmarks, executions with their full configuration, and per-case scores |

### Lifecycles

```
Document.status:       UPLOADED → QUEUED → PROCESSING → COMPLETED | FAILED | QUARANTINED
                       (archiving is separate: archived_at is set, the status is unchanged)
ProcessingJob.status:  QUEUED → PROCESSING → COMPLETED | FAILED
                       (a job awaiting an automatic retry goes back to QUEUED)
ProcessingEvent:       STARTED → SUCCEEDED | SKIPPED | FAILED
```

### Roles and what they may do

| Role | Documents | Administration | Collections | Capabilities reported to the UI |
|---|---|---|---|---|
| `SYSTEM_ADMIN` | Read and manage everything; upload into any department | Everything | Create in any department | all seven |
| `DEPARTMENT_ADMIN` | Read their department's documents and their own; manage their department's | Their department's `FACULTY`/`STAFF`/`STUDENT` accounts; audit log and jobs for their department | Create in their own department; manage that department's collections | `administer_users`, `view_audit_log`, `view_operations`, `create_collections` |
| `FACULTY` | Read their department's documents and their own; manage their own | — | Create in their own department; manage their own | `create_collections` |
| `STAFF`, `STUDENT` | Read their department's documents and their own; manage their own | — | — | none |

- **Read** means the user is the owner, is in the same department, or is a system admin.
- **Manage** (edit, delete, reprocess, new version, archive) means the user is the owner, the
  department admin of that department, or a system admin.
- An account with no department matches nothing, so it sees only its own uploads.

The specification's roles map as follows:

| Specification | This platform |
|---|---|
| `SUPER_ADMIN` | `SYSTEM_ADMIN` |
| `ORG_ADMIN` | `DEPARTMENT_ADMIN` |
| `MANAGER` | `FACULTY` |
| `USER` | `STAFF` |
| `VIEWER` | `STUDENT` |

---

## 7. Document processing pipeline

### Admission, in the request

[`documents/uploads.py`](backend/documents/uploads.py) runs before anything is stored. It refuses:

| File | Response |
|---|---|
| empty | **400** |
| over `MAX_DOCUMENT_SIZE_BYTES` (50 MB) | **413** |
| extension not one of `.pdf .docx .txt .png .jpg .jpeg` | **400** |

- Refusals are audited as `DENIED` and counted in `adi_upload_rejections_total{reason}`.
- nginx caps request bodies at 55 MB, slightly above the application limit, so the application
  produces the error message.
- This check reads the file *name* only. The actual content is sniffed in the worker.

### Stages

Every stage writes a `ProcessingEvent` with its status, message, duration and metadata.

| # | Stage | What it does |
|---|---|---|
| 1 | `INGESTION` | Sniffs the real type from magic bytes rather than trusting the extension (only `.txt` falls back to the extension). Opens a ZIP to confirm it really holds a Word document, because DOCX shares its signature with `.xlsx` and `.zip` |
| 2 | `SCANNING` | Malware scan, if `DOCUMENT_SCANNER` is set. It runs **before** validation on purpose, so a renamed executable is reported as malware rather than as an "unsupported type". See the verdicts below |
| 3 | `VALIDATION` | Checks the supported type, size, page count (`MAX_DOCUMENT_PAGES` = 2000) and that the PDF actually parses. Also checks DOCX archives for decompression bombs (uncompressed size and ratio) |
| 4 | `TEXT_EXTRACTION` | Extracts the PDF text layer per page (pages without one are left for OCR). For DOCX, takes paragraphs **and tables** in document order. For TXT, the text itself |
| 5 | `OCR` | Runs Tesseract on exactly the pages with no text layer, and on images. Renders one page at a time at `OCR_DPI` 200 to bound memory |
| 6 | `LAYOUT_ANALYSIS` | Detects **tables** in the finished page text; every cell is verified to be a substring of the page. Locates **figures** from the PDF's image dictionary, without decoding the images |
| — | *persist* | Writes `DocumentContent`, `PageContent`, tables and figures in one transaction |
| 7 | `CLASSIFICATION` | Runs the configured classifier on the persisted text. If none is configured, records `SKIPPED` |
| 8 | `EXTRACTION` | Runs the configured field extractor, using the schema for the document's type ([below](#information-extraction)) |
| 9 | `INDEXING` | Builds page-bounded chunks (1000 characters, 150 overlap) and a full-text vector, plus embeddings if a provider is configured |
| — | *publish* | Points `DocumentVersion.current_content` at this pass. From here, search answers from it |

**DOCX has no page count.** Pagination is a rendering result. The only boundaries used are the
author's explicit page breaks, and `metadata.pagination` records `explicit_breaks` or `none`.

### Pluggable capabilities

| Setting | Default | When empty |
|---|---|---|
| `DOCUMENT_SCANNER` | empty | `SKIPPED`: the file is recorded as *not scanned*, never as clean |
| `DOCUMENT_CLASSIFIER` | empty | `SKIPPED`. Once set, a missing artifact **fails** the stage, because configuring a classifier means classification is expected |
| `DOCUMENT_FIELD_EXTRACTOR` | empty | `SKIPPED` |
| `DOCUMENT_TABLE_DETECTOR` | `documents.processing.tables.layout.LayoutTableDetector` | Tables are skipped. This capability is on by default because there is nothing to configure |
| `DOCUMENT_FIGURE_DETECTOR` | `documents.processing.figures.service.EmbeddedImageDetector` | Figures are skipped. Also on by default |
| `DOCUMENT_EMBEDDING_PROVIDER` | empty | Chunks are still written and searched **lexically**. Vectors can be backfilled later with `reindex` |

### Malware scanning and quarantine

The bundled `HeuristicScanner` is **not an antivirus**. It has no signature database. Its
verdicts:

| Verdict | Triggered by | Outcome |
|---|---|---|
| `CLEAN` | — | Processing continues |
| `SUSPICIOUS` | Office macros; PDF active content (`/JavaScript`, `/OpenAction`, `/Launch`, embedded files) | **Processing continues.** Findings are recorded and the file can be downloaded. A macro is a capability, not proof of intent |
| `INFECTED` | Executables disguised as documents (PE, ELF, Mach-O), `#!` scripts, the EICAR test string | The document is **QUARANTINED** and never parsed |

What quarantine means:

- `QUARANTINED` is terminal, and deliberately distinct from `FAILED`.
- Reprocess and retry are refused.
- Downloads are refused (403) for everyone except a system administrator.
- The decision is per version. A clean copy is uploaded as a new version.

A scanner that is configured but **unavailable** is retried, never bypassed. ClamAV or a
commercial scanner can sit behind the same `DocumentScanner` protocol, but no such adapter is
written yet.

### Failure handling

- **Permanent by default.** `StageError` covers files such as a corrupt PDF, an oversized
  upload or an unsupported type. These fail the same way on every attempt, so they are not
  retried.
- **Transient errors are retried.** These are `TransientError` and database or connection
  errors. Each gets up to 3 retries with exponential backoff (1, 2 and 4 seconds). While a retry
  is pending, the job is `QUEUED`, not `FAILED`, so a manual retry cannot start a second run
  beside it.
- **Time limits.**
  - At the soft limit (`CELERY_TASK_SOFT_TIME_LIMIT`, 900 s) the run is recorded as `TIMEOUT`
    and is not retried.
  - At the hard limit (960 s) Celery kills the child process. The job is left `PROCESSING`, and
    `recover_stalled_jobs` reclaims it once it has been idle for `STALLED_JOB_MINUTES` (120).
  - A 2,000-page scanned PDF that needs OCR on every page can outrun 900 s. Measure, and raise
    the limit per deployment.
- **Lost workers.** `acks_late` with a prefetch of 1 means a worker *container* that dies leaves
  its message in Redis, which redelivers it after one hour.
- **Idempotent per job.** A retry deletes whatever the same job wrote before. History *across*
  jobs is kept, so two pipeline versions stay comparable. Reprocessing answers **409** while a
  run is active.
- **Broker down at upload.** The job is recorded `FAILED` with `INFRASTRUCTURE_ERROR` rather than
  returning a 500. It can then be retried from Administration → Processing.

**Error codes** (`processing/errors.py`):

| Code | Meaning | Action |
|---|---|---|
| `UNSUPPORTED_FILE_TYPE`, `CORRUPT_DOCUMENT`, `EMPTY_EXTRACTION`, `TOO_MANY_PAGES`, `FILE_TOO_LARGE`, `EMPTY_FILE`, `MISSING_FILE` | The file itself | Ask for a better copy. Not retryable |
| `MALWARE_DETECTED` | Quarantined by the scanner | Review the findings in the timeline. Never retry |
| `TIMEOUT` | Exceeded the soft time limit | Split the document, or raise the limit after measuring |
| `STALLED` | Worker lost; set by `recover_stalled_jobs` | Retry |
| `INFRASTRUCTURE_ERROR` | Database, storage or broker blip | Retried automatically; Retry if it persists |
| `EXTRACTION_FAILED`, `OCR_FAILED`, `CLASSIFICATION_FAILED`, `FIELD_EXTRACTION_FAILED`, `CHUNKING_FAILED`, `SCAN_FAILED`, `UNEXPECTED_ERROR` | A stage failed | Read the event message; check the worker logs |

### Information extraction

Field schemas are **data, not code**: JSON schema packs in
`processing/field_extraction/packs/`.

| Pack | Document type | Fields |
|---|---|---|
| `institutional-v1` (default) | `CIRCULAR` | `circular_number`, `issue_date`, `subject`, `issuing_authority` |
| | `REGULATION` | `regulation_number`, `effective_date`, `academic_year`, `approving_body` |
| | `SYLLABUS` | `course_code`, `course_title`, `credits`, `semester` |
| | `POLICY` | `policy_number`, `effective_date`, `review_date` |
| | `FORM` | `form_number`, `form_title` |
| `invoice-v1` | `OTHER` | `company`, `invoice_number`, `invoice_date`, `total`, `currency` |

The extraction service refuses to store any value whose span does not re-slice out of the stored
page text, so a fabricated value cannot be persisted. The possible outcomes:

| Outcome | How it is recorded |
|---|---|
| Nothing configured, or no schema for this type | The stage is `SKIPPED` |
| Value found | `FOUND`, with page and span |
| Field genuinely absent | `NOT_FOUND` |
| Matched, but failed validation | `INVALID`, with the rule it failed |
| Two different values found | `CONFLICTED`. The value is left empty and every candidate is recorded, never "first wins" |

### Tables and figures

- **Tables** are detected in the stored page text, so they work the same for PDF, OCR, DOCX and
  TXT.
  - Every cell is a verifiable substring of the page.
  - A large table is chunked by region, and each part carries its column labels in metadata.
  - A header row is never quoted alone as an answer.
- **Figures** are recorded as a fact ("page 4 embeds a 640×480 image"), never interpreted.
  - Nothing in the system reads images. OCR does extract any text inside them.
  - When no passage answers a question but a retrieved document has figures, the abstention says
    which page to look at (`MAYBE_IN_A_FIGURE`).

---

## 8. Search

### Chunking

- Chunks are 1,000 characters with a 150-character overlap (`CHUNK_SIZE` and `CHUNK_OVERLAP`;
  changing either requires `reindex`).
- **A chunk never spans pages.**
- Every chunk stores its page number and character span. The service refuses to write a chunk
  whose span does not re-slice out of the page text.

### Embedding providers

| Provider | Setting value | Semantic | Notes |
|---|---|---|---|
| *(none)* | empty | — | Default. Lexical search only |
| Hashing baseline | `documents.processing.embedding.hashing.HashingEmbeddingProvider` | **no** | Deterministic, offline and 384-dimensional. It scores *vocabulary overlap*, not meaning: "car" and "automobile" are unrelated to it. Used in tests and CI |
| Local | `documents.processing.embedding.local.LocalEmbeddingProvider` | yes | `BAAI/bge-small-en-v1.5`, 384-dimensional, on CPU. Loads from the `hf_cache` volume |
| Bedrock Titan V2 | `documents.processing.embedding.bedrock.BedrockTitanEmbeddingProvider` | yes | Needs AWS credentials. **Titan emits 256, 512 or 1024 dimensions, so it needs a schema migration** away from the 384-dimension column before it will load. Contract-tested only |

Embeddings record which provider and version produced them. Search only compares vectors from
the configured provider, so a model change can never mix vector spaces. Content not yet
re-embedded is still found lexically.

### Hybrid retrieval

`search/service.py` runs these steps in order:

1. **Authorization first.** It computes `visible_documents(user)`, then restricts to current
   versions, live extraction passes and non-archived documents. Optional filters narrow further:
   `document_type`, `department`, `document_id` and `collection_id`.
2. **Lexical.** PostgreSQL full-text search over the whole query OR-ed with each term, so both a
   natural sentence and an identifier like `REG/2024/117` match.
3. **Vector.** pgvector cosine similarity over an HNSW index.
4. Each half returns up to `SEARCH_CANDIDATES` (50) candidates. They are fused with
   **reciprocal-rank fusion** (k = 60). Fusion works on ranks, not raw scores, because a text
   rank and a cosine distance are not on comparable scales.

More behaviour to know:

- If the query cannot be embedded (for example, a model failed to load), search continues
  lexically and says so in `notes` rather than returning a 500.
- Search is a POST, so queries stay out of URLs and access logs.
- The audit log records that a search happened, never the query text.

---

## 9. Asking questions: RAG and chat

```
question
  → small-talk check          rag/followup.py      greetings and thanks are never rewritten
  → follow-up resolution      rag/followup.py      borrows terms from the user's OWN earlier questions
  → planning                  agents/planner.py    3+ part questions are split (max RAG_MAX_PLAN_STEPS = 4)
  → retrieval                 search/service.py    permission-filtered hybrid search, depth RAG_RETRIEVAL_DEPTH = 20
  → reranking                 rag/reranking.py | local_rerank.py | bedrock_rerank.py
  → relevance floor           rag/engine.py        too weak → abstain
  → context                   rag/context.py       ≤ 8 passages, ≤ 8000 characters, injection-flagged
  → answerer                  rag/llm.py | claude.py | openai_compat.py
  → grounding                 rag/grounding.py     every claim verified against the passages it cites
  → answer + citations, or an abstention with a reason
```

### Follow-ups and planning

- **Follow-ups.** A question that looks dependent gets terms borrowed from the user's own earlier
  questions, never from the assistant's answers. "Dependent" means one of:
  - a "what about…" opening;
  - a pronoun with nothing to refer to;
  - a single content term.

  More rules:
  - The user's own words remain the **focus**, so the answer addresses what was actually asked.
  - The resolution is stored in `Message.metadata.followup` and shown in the UI as
    "Read as a follow-up".
  - `RAG_FOLLOWUP_HISTORY_TURNS` (2) controls how many earlier questions may lend terms; `0`
    disables follow-up resolution.
- **Planning.**
  - Planning is a regex splitter, not an LLM.
  - Questions with **3 or more** interrogative parts are split. Two-part questions already work
    and are left whole.
  - Each part is answered and grounded independently by the normal engine.
  - Citation markers are renumbered into one sequence. Parts that could not be answered are
    listed, never dropped.
  - There is no synthesis step.
  - Measured: 4-part questions went from a flat abstention to 4 of 4 parts answered.

### Reranking

| Reranker | Setting value | Relevance floor | Degrades to |
|---|---|---|---|
| Lexical (default) | empty, or `documents.rag.reranking.LexicalReranker` | 0.15 | — |
| Local cross-encoder | `documents.rag.local_rerank.LocalCrossEncoderReranker` | Calibrated per model: **0.02** for `BAAI/bge-reranker-base` (the default model), **0.30** for `cross-encoder/ms-marco-MiniLM-L6-v2`, 0.30 for any other model | Lexical, with a note on the answer |
| Bedrock (Cohere Rerank 3.5) | `documents.rag.bedrock_rerank.BedrockReranker` | 0.10 | Lexical |

`RAG_RELEVANCE_FLOOR` overrides every floor. Degradation increments
`adi_rerank_degraded_total`.

### Answerers

| Answerer | `RAG_ANSWERER` | Generative | Streams | Behaviour |
|---|---|---|---|---|
| Extractive (default) | empty | no | no | Quotes up to three sentences **verbatim**. Cannot hallucinate. Will not quote a table header alone, or a flagged passage |
| Claude | `documents.rag.claude.ClaudeAnswerer` | yes | no | Anthropic API (`ANTHROPIC_API_KEY`) or Bedrock (`RAG_ANSWERER_BACKEND=bedrock`, `AWS_BEDROCK_REGION`). Default model `claude-opus-5`, effort `medium`, 60 s timeout, with a server-side refusal fallback |
| OpenAI-compatible | `documents.rag.openai_compat.OpenAICompatibleAnswerer` | yes | **yes** | Any `/chat/completions` server. The presets are below; a custom endpoint needs `RAG_ANSWERER_BASE_URL` and `RAG_ANSWERER_MODEL` |

**OpenAI-compatible presets** (`RAG_ANSWERER_PROVIDER`):

| Preset | Default model | Key variable | Free tier trains on input |
|---|---|---|---|
| `gemini` (default) | `gemini-3.5-flash` | `GEMINI_API_KEY` | yes |
| `groq` | `openai/gpt-oss-120b` | `GROQ_API_KEY` | no |
| `openrouter` | `openrouter/free` | `OPENROUTER_API_KEY` | yes |
| `cerebras` | `gpt-oss-120b` | `CEREBRAS_API_KEY` | no |
| `ollama` | `gpt-oss:20b` at `localhost:11434` | `OLLAMA_API_KEY` (a placeholder is fine) | no |

How the Gemini preset is tuned:

- **Fallback chain.** It falls back to `gemini-3-flash-preview`, then `gemini-3.7-flash`. On a 404,
  429 or 5xx response the next model is tried immediately. The free tier limits requests per
  minute *per model*, so this hides most rate limits. `RAG_ANSWERER_FALLBACK_MODELS` overrides
  the chain, and `none` disables it.
- **Reasoning off.** It sends `reasoning_effort: none`, so hidden reasoning does not consume the
  token budget. Change this with `RAG_ANSWERER_REASONING_EFFORT`.
- Pinning a model with `RAG_ANSWERER_MODEL` disables the chain.

How hosted answerers behave in general:

- **They never break chat.** Any failure — a missing key or SDK, a rate limit, a timeout, a
  malformed response — falls back to the extractive answerer. The answer carries a note, the
  message records `fell_back_from`, and `adi_llm_fallbacks_total{answerer,reason}` counts it.
- Keys are read from the environment only. They are never stored in settings, the database or
  the logs.
- Generative answerers share one system prompt with the grounding checker, so the two cannot
  drift apart.

### Grounding: answers are verified, not trusted

An answer is returned only if all four checks pass:

1. Every `[n]` marker resolves to a supplied passage.
2. It cites at least one passage.
3. **Every figure it states appears in a passage it cites.** Figures include numbers,
   percentages, amounts, dates and identifiers. The comparison ignores formatting: `75%` matches
   `75 percent`, and `85,000` matches `85000`. This is the hard gate.
4. At least 25% of its content words appear in the cited passages (`RAG_MIN_SUPPORT_RATIO`). This
   is a backstop against answering a different subject.

An answer that fails any check is turned into an abstention (`UNGROUNDED`). It is never returned
with a warning attached.

### Abstention reasons

| Constant | Stored reason |
|---|---|
| `NO_QUERY` | empty question |
| `NO_DOCUMENTS` | no authorized documents matched the question |
| `NO_RELEVANT_PASSAGES` | retrieved passages were not relevant enough |
| `EMPTY_CONTEXT` | no passage survived context construction |
| `MODEL_ABSTAINED` | the answerer found nothing in the passages that answered the question |
| `UNGROUNDED` | the answer could not be grounded in the cited passages |
| `MAYBE_IN_A_FIGURE` | no passage answered the question, and the documents retrieved contain figures this system cannot read |

The user-facing text is always *"I couldn't find enough information in the provided documents."*,
with the reason explained in plain language. A planned question whose parts all abstain reports
how many parts could not be answered.

### Prompt-injection defence

- Retrieved text is fenced and labelled as data in the prompt.
- Passages that look like instructions are **flagged but still retrieved**. Dropping them would
  let an attacker hide a document from search.
- Flagged passages are never quoted as an answer, and are reported in `suspected_injections`.
- If every retrieved passage is hostile, the system abstains.
- No model has tools, browsing or write actions.

### General-knowledge replies (opt-in)

`CHAT_GENERAL_KNOWLEDGE=True` is off by default. It is an institution-chosen departure from the
specification's abstention rule (§12.4 / FR-16). When the documents have nothing, chat replies
from the model's general knowledge instead of abstaining.

- **When it applies.** Only after `NO_DOCUMENTS`, `NO_RELEVANT_PASSAGES`, `EMPTY_CONTEXT` or
  `MODEL_ABSTAINED`, and for greetings and small talk (which then skip retrieval). **Never** after
  `UNGROUNDED` or `MAYBE_IN_A_FIGURE`.
- **Where it never applies.** Search and evaluation runs.
- **How it is labelled.** `source: "general"` in the API, in the stored message and in the UI. The
  reply carries no citations.
- **Requirements.** An OpenAI-compatible answerer (only it implements `complete()`). The last 6
  turns are sent as context.
- **On provider failure,** the abstention stands.

### Streaming

`POST /api/v1/chat/stream/` runs the same chat turn and sends Server-Sent Events:

| Event | Payload | Meaning |
|---|---|---|
| `delta` | `{text}` | Words as the model writes them. **Provisional**, because grounding still runs afterwards |
| `reset` | `{}` | Discard what was shown so far |
| `done` | the full `/chat/` response | The **authoritative** answer, with citations |
| `error` | `{detail}` | The turn failed |

- Validation, throttling and conversation ownership fail as ordinary JSON 4xx responses *before*
  streaming starts.
- A `: keep-alive` comment is sent every 15 s.
- The response sets `X-Accel-Buffering: no`, so nginx does not hold the words back.
- Only the OpenAI-compatible answerer emits `delta` events. With the extractive or Claude
  answerer, the stream sends only `done`.

### Conversations and feedback

- Conversations are **owner-only**. Another user's conversation id returns 404.
- A conversation keeps the citation text the user was shown, even if their access changes later.
- Each answer can be rated `HELPFUL`, `NOT_HELPFUL` or `INCORRECT` through
  `POST /api/v1/messages/{id}/feedback/`. Ratings feed `adi_answer_feedback_total`, an online
  quality signal.

---

## 10. Collections

A collection is a named set of documents within one department (§14, §15.2).

- **It narrows; it never grants access.**
  - Search and chat apply the caller's document permissions first, and `collection_id` after.
  - A collection the caller cannot see narrows the results to nothing.
- **Who can create one:** system admins (in any department), and department admins and faculty
  who have a department (in their own). This is the `create_collections` capability.
- **Who can see one:** system admins see all. Everyone else sees their own department's
  collections and the ones they created.
- **Who can manage one** (rename, delete, change membership): the creator, the department's
  admin, or a system admin.
- **Membership rules.**
  - Only documents the caller can read *and* that belong to the collection's department can be
    added.
  - Anything else gets one 404 that lists the refused ids.
  - Each request handles at most 500 ids.
  - Deleting a collection leaves its documents untouched.
- **What a caller sees.** Member lists and counts include only documents the caller may read.
- **Audit.** Creation, updates and deletion are audited.

---

## 11. Security model

The full model is in [docs/security/model.md](docs/security/model.md). In brief:

### Identity

- **Browser sessions.**
  - httpOnly cookies, `Secure` outside DEBUG, `SameSite=Lax`.
  - Cached in Redis, with an 8-hour absolute lifetime.
  - Login rotates the session key and **enforces CSRF even though the caller is anonymous**.
  - No credential is readable by JavaScript, and nothing is kept in `localStorage`.
- **API tokens** (for scripts) expire after `TOKEN_TTL_HOURS` (12). An expired token is deleted
  on first use.
- **Password changes** end every other session and revoke all API tokens, however the password
  is changed: through the API, Django's admin or `changepassword`.
- **Deactivation** ends the account's sessions and revokes its tokens. Accounts are never deleted
  through the API, so their audit history keeps its subject.
- **Brute-force limits.**
  - 10 attempts per minute per address *and* account.
  - 100 per hour per account across all addresses.
  - Edge limits at nginx on top of both.
- **Account takeover is blocked structurally.**
  - Nobody can change their own role or deactivate themselves.
  - Department admins cannot assign administrative roles or edit other admins.
  - A role-only system admin cannot edit a superuser.

### Authorization

- **No existence leaks.**
  - A document the caller may not read returns a **404** that is byte-identical to an unused
    id's, on every action including writes.
  - **403** means the caller can see the document but may not perform the action.
  - Both refusals are audited as `DENIED`.
- **Retrieval is filtered before similarity.** Citations can only name readable documents.
- **The frontend decides nothing.** It hides what a user cannot do, but every endpoint
  re-checks.

### Files

- Size, extension and emptiness are checked before anything is stored, and the content is
  sniffed by magic bytes.
- Media is **never served statically**.
  - Original bytes are reachable only through the authenticated, audited
    `/api/v1/documents/{id}/download/`.
  - nginx deliberately has no `/media/` location.
  - S3 storage uses private ACLs.
- Previews are fetched with the session and rendered from `blob:` URLs. Their type is pinned from
  the validated extension, never the uploader's MIME type.

### AI and transport

- **AI.** See [§9](#9-asking-questions-rag-and-chat): permission-filtered retrieval, documents
  treated as data, grounding on every answer, and no tools.
- **Answer rendering.** Answers go through a tiny Markdown renderer that never produces HTML from
  a string.
- **TLS** terminates at nginx (TLS 1.2+). Django redirects to HTTPS and trusts
  `X-Forwarded-Proto` from nginx.
- **Response headers.** A strict Content-Security-Policy (`script-src 'self'`), plus
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` and
  COOP.
- **HSTS** is off until an operator sets `SECURE_HSTS_SECONDS`, because it is close to
  irreversible.

### Audit

The audit log records actor, action, result, document and metadata for:

- every document view, download (with its version), write, archive, restore, reprocess and
  version upload;
- every search and chat, **without the query text**;
- logins and failed logins, logouts and password changes;
- account and department changes, collection changes and job retries.

Denials are recorded as well as approvals.

- A deleted document's audit rows keep its id and department, and the delete row keeps its
  title.
- Department admins see their members' denied attempts on other departments' documents with the
  document id and title withheld.

### Logging

JSON logs go through one formatter that **redacts document content**, whatever the caller passed:

- fields named for content (`question`, `answer`, `text`, `password`, …) are replaced wholesale;
- any other string over 120 characters is replaced (not truncated), with only its length kept;
- for `adi.*` loggers, anything interpolated into the message is dropped.

Metric labels are bounded vocabularies: never ids, users or query text.

### Known security gaps

These are documented and accepted, or awaiting an institutional decision:

- there is no antivirus engine;
- a new upload can be downloaded before its scan completes;
- Redis has no password (it is reachable only on the compose network);
- `/django-admin/` is reachable from the internet, rate-limited at 1 request/s;
- there is no encryption at rest in the application (it relies on the disk or bucket);
- there is no SSO or MFA;
- dependency audits in CI are advisory only.

---

## 12. API

The full reference is [docs/api/reference.md](docs/api/reference.md).

### Conventions

- **Base path.** **`/api/v1/`** is canonical, and is what the web app calls. Every endpoint is
  also served at `/api/…` as a permanent alias for older scripts.
- **Authentication.**
  - *Browsers*: `GET /api/v1/auth/csrf/`, then `POST /api/v1/auth/login/` with `X-CSRFToken`.
    Every unsafe request then carries `X-CSRFToken`.
  - *Scripts*: `POST /api/v1/auth/token/`, then send `Authorization: Token <key>`.
- **Errors.** DRF shapes: `{"detail": …}` or `{"field": ["…"]}`.

  | Status | Meaning |
  |---|---|
  | 401 | Not signed in |
  | 404 | Not found, **or** not readable by the caller |
  | 403 | Readable, but the action is not allowed |
  | 409 | Conflicts with current state: a run already active, an archived document, a pending idempotent request |
  | 413 | File too large |
  | 422 | Idempotency key reused with different content |
  | 429 | Throttled |

- **Pagination.** Page-number pagination, 50 per page (`?page=N`).
- **Request bodies.** Document endpoints take **multipart/form data only**, PATCH included.
  Everything else takes JSON.
- **Idempotency.** `POST /documents/` and `POST /documents/{id}/versions/` accept an
  `Idempotency-Key` header of 1–255 printable characters.
  - Keys are per user and kept for 24 hours.
  - A retry with the same key returns the first result: **202** with
    `Idempotent-Replayed: true`.
  - The same key with a different file or title is **422**.
  - The same key while the first request is still running is **409**.
- **Correlation.** Every response carries `X-Request-ID`. A caller-supplied id is adopted after
  sanitisation.

### Endpoints

Paths are relative to `/api/v1/`.

**Identity and shell**

| Method | Path | Notes |
|---|---|---|
| GET | `auth/csrf/` | Sets the CSRF cookie |
| POST | `auth/login/` · `auth/logout/` · `auth/password/` | Session login, logout, own password change |
| POST | `auth/token/` | Script login; returns an expiring token |
| GET | `auth/me/` | Profile, role, department, `capabilities` |
| GET | `departments/` | Active departments |
| GET | `dashboard/` | Counts and lists over the caller's visible documents |

**Documents** (read = owner, same department or system admin; manage = owner, the department's
admin or system admin)

| Method | Path | Notes |
|---|---|---|
| GET · POST | `documents/` | List (see [discovery](#document-discovery)) · upload → **202** |
| GET · PATCH · DELETE | `documents/{id}/` | Detail (includes `can_manage`) · edit title/type · permanent delete (versions, content, index and files) |
| POST | `documents/{id}/archive/` · `restore/` | Archived documents leave search, chat and the default listing |
| GET · POST | `documents/{id}/versions/` | List versions · upload a new version → **202** (409 if archived) |
| GET | `documents/{id}/download/[?version=N]` | Original bytes, audited. 403 for a quarantined version unless the caller is a system admin |
| POST | `documents/{id}/reprocess/` | **202** with the job; **409** with the running job if one is active |
| GET | `documents/{id}/processing/` | Every job with its stage events |
| GET | `documents/{id}/content/` · `classification/` · `extraction/` · `tables/` · `figures/` · `chunks/` | What the pipeline produced (the live pass) |
| GET | `documents/{id}/source/?chunk=C` | Citation evidence: page text, span, `is_current` |

**Search, chat, collections, evaluation**

| Method | Path | Notes |
|---|---|---|
| POST | `search/` | `{query, limit ≤ 50, document_type?, department?, document_id?, collection_id?}` |
| POST | `chat/` | `{question, conversation_id?, document_type?, department?, document_id?, collection_id?}` → answer, citations, abstention reason, grounding, retrieval counts, model, plan, timings, notes, `resolved_question`, `source` (`documents` / `general` / `none`) |
| POST | `chat/stream/` | The same turn as Server-Sent Events ([§9](#streaming)) |
| GET | `chat/conversations/` | The caller's conversations |
| GET · PATCH · DELETE | `chat/conversations/{id}/` | Owner only |
| POST | `messages/{id}/feedback/` | `{rating, comment?}` |
| GET · POST | `collections/` | List · create (curators) |
| GET · PATCH · DELETE | `collections/{id}/` | Read · rename · delete |
| POST · DELETE | `collections/{id}/documents/` | `{document_ids: [...]}` add · remove |
| GET | `evaluations/`, `evaluations/{id}/`, `{id}/results/`, `{a}/compare/{b}/` | Evaluation runs, scoped by the dataset's department |

**Administration** (system admins everywhere; department admins scoped to their department)

| Method | Path | Notes |
|---|---|---|
| GET · POST · PATCH | `admin/users/[{id}/]` | Filters `search`, `role`, `department`, `is_active`. Each row carries `can_manage`. No DELETE: deactivate instead |
| POST | `admin/users/{id}/set-password/` | Ends the user's sessions and tokens |
| GET | `admin/users/roles/` | Roles the caller may assign |
| GET · POST · PATCH | `admin/departments/[{id}/]` | Writes by system admins only. A rename rewrites every reference atomically |
| GET | `admin/audit/` | Filters `action`, `result`, `user`, `document`, `since`, `until` |
| GET · POST | `admin/jobs/` · `admin/jobs/{id}/retry/` | Filters `status`, `error_code`, `stalled=true` |
| GET | `admin/system/` | Dependency health, queue depths, counts, AI configuration (names only, never secrets). System admins only |

**Outside the API**

| Path | Purpose |
|---|---|
| `/health/` | Liveness: process up and database reachable. Does not depend on Redis |
| `/ready/` | Readiness: database, cache and broker. 503 with per-component status |
| `/metrics` | Prometheus, with no trailing slash. nginx answers 404 for it; `METRICS_ALLOWED_IPS` can restrict it further |
| `/django-admin/` | Django admin, for superusers. `/admin/*` belongs to the web app's own screens |

### Example

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/token/ \
  -d "username=alice&password=secret" | python -c "import sys,json; print(json.load(sys.stdin)['token'])")

curl -X POST http://localhost:8000/api/v1/documents/ -H "Authorization: Token $TOKEN" \
  -H "Idempotency-Key: upload-regs-2024" \
  -F "title=Exam Regulations 2024" -F "document_type=REGULATION" -F "file=@regulations.pdf"

curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/documents/1/processing/

curl -X POST http://localhost:8000/api/v1/chat/ -H "Authorization: Token $TOKEN" \
  -H "Content-Type: application/json" -d '{"question": "What is the attendance minimum?"}'
```

### Document discovery

`GET /api/v1/documents/` supports narrowing and ordering. The implementation is
`documents/api/discovery.py`. Every parameter is optional, and they compose with each other and
with `page`:

| Parameter | Matches |
|---|---|
| `search` | Title, or the current version's file name. Case-insensitive substring |
| `status` | `UPLOADED`, `QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED`, `QUARANTINED` |
| `document_type` | `GENERAL`, `REGULATION`, `POLICY`, `SYLLABUS`, `CIRCULAR`, `FORM`, `OTHER` |
| `department` | Exact department name |
| `owner` | A user id, or `me` |
| `created_after` / `created_before`, `updated_after` / `updated_before` | A date (meaning the whole day) or a full ISO-8601 datetime |
| `archived` | `false` (default), `true` or `all` |
| `ordering` | Up to 3 of `id`, `title`, `status`, `document_type`, `created_at`, `updated_at`, each optionally prefixed with `-`. Default `-created_at`. `id` is always appended as a tiebreak, so pages never skip or repeat rows |

- An invalid value is a **400** naming the field.
- `ordering` is resolved against a fixed allowlist, never passed to the ORM raw.
- **Filters only ever narrow** what `visible_documents` already returns. Filtering by another
  department returns an empty list, never an error that would confirm the department exists.
- The list response omits the storage key and checksum. The detail endpoint has them.

**Indexes, measured.** Two indexes back discovery: `(department, -created_at)` and `status`.
Measured with `EXPLAIN ANALYZE` on tens of thousands of rows:

| Query | Before | After |
|---|---|---|
| Authorization-scoped listing | 44–76 ms (parallel sequential scan) | ~8 ms |
| `+ status` filter | 35 ms | ~4 ms |
| `+ search` | 28 ms | ~3 ms |
| System admin, `?department=` filter | — | ~1.8 ms (index scan in `ORDER BY` order) |

No index was added for `search`: `ILIKE '%term%'` cannot use a B-tree. A system admin's unfiltered
search is a sequential scan, measured at ~57 ms over ~86,000 rows. `pg_trgm` is the next step if
that ever matters. The full analysis is in the
[design history](docs/architecture/design-history.md#document-discovery).

---

## 13. Web application

React 19 + TypeScript + Vite 6, with no UI framework (one stylesheet). It talks to the API **on the
same origin**:

- in development, Vite proxies `/api`, `/health`, `/ready`, `/django-admin` and `/static` to
  `VITE_PROXY_TARGET` (default `http://localhost:8000`);
- in production, nginx serves the build and proxies the API.

### Screens

| Path | Screen | Shown to |
|---|---|---|
| `/` | **Dashboard**: counts, recent documents, failures needing attention, recent conversations, a question box | everyone |
| `/documents` | **Documents**: search titles; filter by status, type, department, owner and archived; sort; paginate. State lives in the URL | everyone |
| `/documents/:id` | **Document**: overview and classification, preview (PDF and images, never for quarantined files), text by page, fields with source spans, tables, figures, versions, processing timeline. Edit, new version, reprocess, archive and delete for managers | everyone (the server enforces access) |
| `/search` | **Search**: hybrid search that highlights terms and opens the exact passage. The query is kept out of the URL | everyone |
| `/ask`, `/ask/:conversationId` | **Ask**: conversations with rename and delete; streamed answers; numbered citations that open the evidence (span tinted, a warning if the document has changed since); plain-language abstentions; follow-up and plan display; a "general knowledge" label; feedback; an optional "Ask within" collection | everyone |
| `/collections` | **Collections**: create, curate and delete | everyone; creating needs `create_collections` |
| `/account` | **Account**: profile and password change | everyone |
| `/admin/users` | **Users**: scoped for department admins | `administer_users` |
| `/admin/departments` | **Departments**: registry with cascading rename | `administer_departments` (read-only with `administer_users`) |
| `/admin/audit` | **Audit log** | `view_audit_log` |
| `/admin/processing` | **Processing**: failed, running and stalled runs, with retry | `view_operations` |
| `/admin/system` | **System status**: dependency health, queues, counts, AI configuration | `view_system_status` |

### How it works

- **Auth** (`src/auth/AuthContext.tsx`).
  - On load it calls `/api/v1/auth/me/`.
  - 401 means signed out. A network error or 5xx shows "server unreachable" rather than signing
    the user out.
  - A CSRF cookie is fetched on demand, and `X-CSRFToken` is re-read from the cookie on every
    unsafe request, because login rotates it.
- **API client** (`src/api/client.ts`).
  - Typed same-origin `fetch` that never swallows errors.
  - Uploads go through XHR, for progress reporting.
  - Previews and downloads go through authenticated blobs.
- **Chat stream.** `streamChat()` POSTs to `/api/v1/chat/stream/` and reads `response.body`
  through a `TextDecoderStream`. It deliberately does not use `EventSource`, which cannot POST.
  A stream that ends without `done` is an error.
- **Rendering.** Answers render through a tiny Markdown renderer in `src/lib/markdown.tsx` that
  never produces HTML and renders no links or images, because answer text derives from untrusted
  documents.
- **Code splitting.** Every page except Login is lazy-loaded, so the admin screens are separate
  chunks.

### Checks

```bash
cd frontend
npm run typecheck      # tsc --noEmit
npm test               # Vitest + Testing Library (jsdom): 55 tests
npm run build          # typecheck + production bundle in dist/
```

More detail is in [frontend/README.md](frontend/README.md).

---

## 14. Configuration reference

All configuration is environment variables, read in
[`backend/config/settings.py`](backend/config/settings.py) from the repository-root `.env`.
[`.env.example`](.env.example) is the commented template.

Under `manage.py test`, the AI settings are **pinned to the offline baselines** regardless of
`.env`: hashing embeddings, lexical reranker, extractive answerer, general knowledge off. Tests
that need a provider override it explicitly.

### Required in production

| Variable | Why |
|---|---|
| `DJANGO_SECRET_KEY` | With DEBUG off, the app refuses to start if it is empty or a shipped placeholder (`change-me`, `unsafe-development-key`). Use 50+ random characters |
| `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | The public hostname, and `https://<hostname>` |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | No defaults |
| `NGINX_SERVER_NAME`, `CERTBOT_EMAIL` | Select and obtain the TLS certificate |
| `GRAFANA_PASSWORD` | Compose refuses to start without it |
| `APP_PORT` | The base compose file, which the production overlay merges, has no default for it; `.env.example` sets `8000` |

The production overlay forces `DJANGO_DEBUG=False` and `DRF_NUM_PROXIES=1`.

### Core, database, cache and queue

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `False` (the example sets `True`) | Debug mode; also controls secure cookies and the SSL redirect |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `localhost` / `5432` | Use `db` inside compose |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_CACHE_URL` | `redis` / `6379` / `redis://redis:6379/2` | Cache: sessions, throttles, idempotency keys |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://redis:6379/0` / `/1` | Task queue |
| `CELERY_TASK_SOFT_TIME_LIMIT` / `CELERY_TASK_TIME_LIMIT` | `900` / `960` | Per-job time limits, in seconds |
| `CELERY_PREFETCH_MULTIPLIER` | `1` | Sets `CELERY_WORKER_PREFETCH_MULTIPLIER` |
| `STALLED_JOB_MINUTES` | `120` | When a `PROCESSING` job is presumed lost |
| `CELERY_CONCURRENCY` / `CELERY_INDEX_CONCURRENCY` | `2` / `6` | Production `worker` and `worker-index` concurrency |
| `GUNICORN_WORKERS` / `GUNICORN_THREADS` / `GUNICORN_TIMEOUT` | `3` / `2` / `120` | Production web server |
| `USE_X_FORWARDED_PROTO` | `True` | Trust nginx's `X-Forwarded-Proto` |
| `SECURE_SSL_REDIRECT` | `not DEBUG` | HTTPS redirect (lifted under the test runner) |
| `SECURE_HSTS_SECONDS` / `_INCLUDE_SUBDOMAINS` / `_PRELOAD` | `0` / `True` / `False` | HSTS. Off until the domain owner decides |
| `EMAIL_HOST` (+ `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`) | empty (console backend) | Nothing sends mail yet; set this so future mail is not lost |

### Sessions, CORS and throttling

| Variable | Default | Purpose |
|---|---|---|
| `SESSION_COOKIE_AGE` | `28800` (8 h) | Absolute session lifetime |
| `TOKEN_TTL_HOURS` | `12` | API token lifetime |
| `CORS_ALLOWED_ORIGINS` | empty (same-origin only) | Under DEBUG the Vite origin `:5173` is allowed automatically. Credentials are never sent cross-origin |
| `DRF_NUM_PROXIES` | `0` (production: `1`) | Trusted proxies when reading `X-Forwarded-For`, so throttles key on the real client |
| `THROTTLE_LOGIN` / `THROTTLE_LOGIN_ACCOUNT` | `10/minute` / `100/hour` | Login, token and password endpoints, per address+account and per account |
| `THROTTLE_UPLOAD` / `THROTTLE_SEARCH` / `THROTTLE_CHAT` | `60/hour` / `120/hour` / `60/hour` | Per-endpoint limits. Upload covers create, new version and reprocess |
| `THROTTLE_USER` / `THROTTLE_ANON` | `3600/hour` / `30/hour` | Catch-alls |

### Storage

| Variable | Default | Purpose |
|---|---|---|
| `AWS_STORAGE_BUCKET_NAME` | empty (local `media_data` volume) | Switches every file field to S3. Needs an image built with `INSTALL_S3=true` |
| `AWS_S3_REGION_NAME`, `AWS_S3_ENDPOINT_URL` | empty | Region; endpoint for MinIO or Ceph |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | SDK chain | Credentials |
| `BACKUP_ROOT` / `BACKUP_RETENTION` | `<repo>/backups` (production: `/backups`) / `7` | Where `manage.py backup` writes, and how many backups it keeps |

**Fixed in code, not the environment:** `MAX_DOCUMENT_SIZE_BYTES` (50 MB), `MAX_DOCUMENT_PAGES`
(2000), `OCR_DPI` (200) and the embedding width (384, `documents/models/chunk.py`).

### Processing and search

| Variable | Default | Purpose |
|---|---|---|
| `DOCUMENT_SCANNER` / `DOCUMENT_SCANNER_RULESET` | empty | Malware scanner ([§7](#malware-scanning-and-quarantine)) |
| `DOCUMENT_CLASSIFIER` / `DOCUMENT_CLASSIFIER_ARTIFACT` | empty / `ai/classification/artifacts/document-classifier/v0.1.0/model.joblib` | Classifier and its trained artifact |
| `DOCUMENT_FIELD_EXTRACTOR` / `DOCUMENT_FIELD_SCHEMA_PACK` | empty / bundled `institutional-v1.json` | Field extraction |
| `DOCUMENT_TABLE_DETECTOR` / `DOCUMENT_FIGURE_DETECTOR` | bundled detectors (on) | Layout analysis; empty disables |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1000` / `150` | Chunking; changing either requires `reindex` |
| `DOCUMENT_EMBEDDING_PROVIDER` | empty (lexical only) | [§8](#embedding-providers) |
| `EMBEDDING_LOCAL_MODEL` / `_BATCH_SIZE` / `_DEVICE` / `_THREADS` | `BAAI/bge-small-en-v1.5` / `32` / `cpu` / `0` | Local embedder. Set threads to cores ÷ worker concurrency |
| `EMBEDDING_BEDROCK_MODEL` / `_CONCURRENCY` / `_TIMEOUT` / `_RETRIES` | `amazon.titan-embed-text-v2:0` / `8` / `30` / `6` | Bedrock embedder |
| `SEARCH_CANDIDATES` | `50` | Candidates per retrieval half before fusion |
| `AI_WARMUP` | `True` | Load models when gunicorn and Celery workers start |
| `HF_HUB_OFFLINE` | unset | `1` means models load only from the cache; set it after `warm_models` |

### Answering and chat

| Variable | Default | Purpose |
|---|---|---|
| `RAG_RERANKER` | empty (lexical) | [§9](#reranking) |
| `RAG_RERANKER_LOCAL_MODEL` / `_BATCH_SIZE` / `_DEVICE` | `BAAI/bge-reranker-base` / `16` / `cpu` | Local cross-encoder (MiniLM is recommended on CPU) |
| `RAG_RERANKER_MODEL` / `_TIMEOUT` / `_RETRIES` | `cohere.rerank-v3-5:0` / `15` / `3` | Bedrock reranker |
| `RAG_RELEVANCE_FLOOR` | unset (per reranker) | Overrides every floor |
| `RAG_RETRIEVAL_DEPTH` / `RAG_MAX_PASSAGES` / `RAG_CONTEXT_BUDGET` | `20` / `8` / `8000` | Retrieval and context sizing |
| `RAG_MIN_SUPPORT_RATIO` | `0.25` | Grounding backstop |
| `RAG_MAX_PLAN_STEPS` | `4` | Question planning; `1` disables it |
| `RAG_FOLLOWUP_HISTORY_TURNS` | `2` | Follow-up resolution; `0` disables it |
| `RAG_ANSWERER` | empty (extractive) | [§9](#answerers) |
| `RAG_ANSWERER_MODEL` / `_TIMEOUT` / `_RETRIES` | provider default / `60` / `2` | Keep `TIMEOUT × (RETRIES + 1)` under nginx's 120 s |
| `RAG_ANSWERER_BACKEND` / `RAG_ANSWERER_EFFORT` | `api` / `medium` | Claude: `api` or `bedrock`; reasoning effort |
| `ANTHROPIC_API_KEY`, `AWS_BEDROCK_REGION` (falls back to `AWS_REGION` / `AWS_DEFAULT_REGION`) | — | Claude credentials; the region is required for every Bedrock component |
| `RAG_ANSWERER_PROVIDER` | `gemini` | OpenAI-compatible preset |
| `RAG_ANSWERER_BASE_URL` / `RAG_ANSWERER_API_KEY` / `RAG_ANSWERER_KEY_ENV` / `RAG_ANSWERER_PROVIDER_TRAINS_ON_INPUT` | — | A custom OpenAI-compatible endpoint |
| `RAG_ANSWERER_TEMPERATURE` / `RAG_ANSWERER_FALLBACK_MODELS` / `RAG_ANSWERER_REASONING_EFFORT` | `0.0` / preset chain / preset value | Generation tuning |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `CEREBRAS_API_KEY`, `OLLAMA_API_KEY` | — | Preset keys, read from the environment only |
| `CHAT_GENERAL_KNOWLEDGE` / `CHAT_GENERAL_TEMPERATURE` / `CHAT_GENERAL_MAX_TOKENS` | `False` / `0.3` / `4096` | General-knowledge replies ([§9](#general-knowledge-replies-opt-in)) |

### Observability

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Also governs Celery, which uses Django's logging |
| `METRICS_ALLOWED_IPS` | empty (any address) | Restricts `/metrics` |
| `PROMETHEUS_MULTIPROC_DIR` | unset (production: `/tmp/prometheus`) | Aggregates metrics across gunicorn and Celery processes |
| `CELERY_METRICS_PORT` | unset (production: `9100`) | Each worker container serves `/metrics` on this port |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset (tracing off) | Turns on OpenTelemetry. Needs `INSTALL_OTEL=true` |
| `OTEL_SERVICE_NAME`, `DEPLOY_ENVIRONMENT` | `adi-api` / `adi-worker`, `development` | Span resource attributes |
| `PROMETHEUS_RETENTION` | `15d` | Prometheus storage retention |

### Build and compose

| Variable | Default | Purpose |
|---|---|---|
| `INSTALL_ML` / `INSTALL_LOCAL` / `INSTALL_LLM` | `false` (production: all `true`) | Optional image layers ([§5](#turning-on-the-optional-ai-layers)) |
| `INSTALL_S3` / `INSTALL_OTEL` | `false` | Production overlay only. The development compose file does not pass these |
| `FRONTEND_PORT` | `5173` | Development frontend container |

---

## 15. Management commands

Run each as `docker compose exec backend python manage.py <command>`. In production, use
`docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend python manage.py
<command>`.

| Command | What it does | Key flags |
|---|---|---|
| `createsuperuser` | The first administrator (Django built-in); becomes `SYSTEM_ADMIN` | — |
| `warm_models` | Downloads, then loads, the configured embedder and cross-encoder | — |
| `reindex` | Queues re-chunking and re-embedding on the `index` queue. Refuses to run with no embedding provider, because that would delete vectors and write none | `--dry-run`, `--stale-only`, `--department`, `--limit`, `--sync` |
| `recover_stalled_jobs` | Finds `PROCESSING` jobs idle longer than `STALLED_JOB_MINUTES`. Reports only, unless `--apply` | `--apply`, `--requeue` |
| `backup` | `pg_dump` custom-format archive, verified by reading it back. Writes a manifest with SHA-256 and migration state | `--prune`, `--include-media`, `--label`, `--output-dir` |
| `restore` | Checks the manifest checksum, then restores. Only reports unless `--yes` | `<dump>`, `--yes`, `--skip-checksum` |
| `seed_evaluation` | Ingests a benchmark's corpus through the real pipeline and resolves the template's slugs to ids | `--template`, `--output`, `--user` (`ci-evaluator`), `--prune` |
| `evaluate` | Runs a benchmark through `rag.ask` as a user; compares runs; gates on a baseline | `--load`, `--dataset`, `--dataset-version`, `--as-user`, `--label`, `--compare-to`, `--baseline`, `--write-baseline`, `--fail-on-regression`, `--tolerance`, `--gate-latency` |
| `ingest_corpus` | Bulk-ingests QS-OCR pages or a directory of `.txt` files, for scale testing | `--owner`, `--source`, `--limit`, `--department`, `--no-process`, `--dry-run` |
| `seed_e2e` | Creates the browser-test accounts and departments. **Refuses** any database whose name lacks `e2e`, `test` or `ci` | `--password` (≥ 12 characters) |
| `benchmark_extraction` | Compares the `pdf_text`, `ocr` and `auto` extraction approaches on a folder; uses `.txt` ground truth when present | `<dataset>`, `--approach`, `--json` |

There is **no Celery beat**. Schedule these with cron on the host:

```
*/15 * * * *  … python manage.py recover_stalled_jobs --apply --requeue
0 2 * * *     … python manage.py backup --prune        # then copy ./backups off the host
```

---

## 16. Testing

Four layers, each catching what the others cannot:

| Layer | Where | Runs against | Command |
|---|---|---|---|
| Backend unit + integration (1,080 tests) | `backend/**/tests*.py` | **Real** PostgreSQL + pgvector, Celery eager | `docker compose run --rm -T -e LOG_LEVEL=ERROR backend python manage.py test --noinput` |
| AI workspace (88 tests) | `ai/classification/tests/` | No Django or database | `PYTHONPATH=. python -m unittest discover -s ai/classification/tests -t .` |
| Frontend unit + component (55 tests) | `frontend/src/**/*.test.ts(x)` | jsdom, mocked API | `cd frontend && npm test` |
| Browser end-to-end (8 tests) | `frontend/e2e/platform.spec.ts`, `csp.spec.ts` | A **live** stack: Django, Celery, PostgreSQL, Redis, models | see below |

### What the backend suite pins

- The suite is organised by capability: `tests_security.py`, `tests_lifecycle.py`, `tests_rag.py`,
  `tests_chat_streaming.py`, `tests_collections.py` and so on.
- **Regression tests are written to fail without their fix.** Many say so in their docstring,
  along with the measured failure they replaced.
- Hosted providers are exercised with stub clients (`tests_providers.py`,
  `tests_claude_answerer.py`).
- Concurrency tests use real threads and transactions.

### The browser suite

`platform.spec.ts` walks the whole product in one serial run:

1. A student uploads a text file, a PDF and a scanned image, and watches them process (OCR
   included).
2. Search opens the evidence passage.
3. Ask gives a cited answer, a follow-up and an abstention.
4. A faculty member of another department is shown nothing.
5. A department admin creates an account and finds the denied attempt in the audit log.
6. A system admin checks every dependency.

`csp.spec.ts` visits every screen and preview, and fails on any Content-Security-Policy violation.
It skips itself against the Vite dev server, which sends no policy.

It needs a **disposable database** and the `seed_e2e` accounts:

```bash
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" adi_e2e'
docker compose run --rm -e POSTGRES_DB=adi_e2e backend python manage.py migrate
export E2E_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(16))')"
docker compose run --rm -e POSTGRES_DB=adi_e2e backend python manage.py seed_e2e --password "$E2E_PASSWORD"
docker compose run -d --name adi-e2e-backend -p 8001:8000 -e POSTGRES_DB=adi_e2e backend python manage.py runserver 0.0.0.0:8000
docker compose run -d --name adi-e2e-worker -e POSTGRES_DB=adi_e2e worker celery -A config worker --queues=ingest,index --concurrency=2
cd frontend && VITE_PROXY_TARGET=http://localhost:8001 npx vite &
E2E_PASSWORD="$E2E_PASSWORD" npx playwright test
```

Playwright environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `E2E_BASE_URL` | `http://localhost:5173` | Frontend under test |
| `E2E_PASSWORD` | — | Password of the seeded accounts. Without it, every spec skips |
| `E2E_CHANNEL` | `chrome` | `chrome`, `msedge`, or `chromium` (Playwright's bundled browser, used in CI) |
| `E2E_IGNORE_HTTPS_ERRORS` | off | `1` for a local stack with a self-signed certificate |

### Rules for tests

- **Never point tests, benchmarks or restore drills at the development database** (the
  `postgres_data` volume). Use a separately named database, such as `adi_e2e`.
- Run the Django suite serially. `--parallel` hides real failures.
- Use `LOG_LEVEL=ERROR` to keep the output readable.

The full guide is [docs/development/testing.md](docs/development/testing.md).

---

## 17. Evaluation

The project rule is that **nothing claims a number without a scored set**.

### Datasets

All evaluation data is **synthetic**. It was written for this repository; nothing was copied from
a real institution.

| Dataset | Template | Cases | Corpus | Role |
|---|---|---|---|---|
| `institutional-seed` v1 | `evaluation/benchmark.template.json` | 13 (3 must abstain) | 5 documents | CI gate. Fast and deterministic, but saturated: every configuration scores recall@1 = 1.0 |
| `institutional-v2` v2 | `evaluation/benchmark-v2.template.json` | 57 (8 must abstain) | 9 documents | CI gate. Adds **paraphrase** and **near-miss** cases, so it can see a retrieval that got worse |
| `institutional-calibration` c1 | `evaluation/calibration-c1.template.json` | 37 (7 must abstain) | 9 documents | **Development set** for choosing thresholds. Never reported as quality |

- The corpus is `evaluation/corpus/*.txt`: academic regulations, examination policy, fee
  structure, hostel, library, grievance, IT acceptable use, placement and scholarship rules.
- Templates refer to documents by slug (`@fee-structure`). `seed_evaluation` ingests the corpus
  **through the real pipeline** and writes the resolved `benchmark*.json`. Those files are
  git-ignored, because the ids are per-database.
- **Loaded datasets are immutable.** A change is a new version, so scores over time compare like
  with like.

### Metrics

Defined in `documents/evaluation/metrics.py`:

| Layer | Metrics |
|---|---|
| Retrieval | Recall@k, Precision@k, MRR, MAP and nDCG@k, over documents, for answerable cases |
| Answers | coverage, abstention accuracy, **false abstentions** (declined an answerable case), **missed abstentions** (answered one that should be declined), groundedness |
| Citations | citation accuracy, source coverage |
| System | latency p50 / p95 / max |

How the scoring works:

- Abstention is scored separately; it is never counted as a "wrong answer".
- Answer-quality metrics are averaged over answered cases only.
- `token_overlap` is reported as a weak signal, never as correctness.

### The regression gate

```bash
python manage.py seed_evaluation
python manage.py evaluate --load evaluation/benchmark.json --dataset institutional-seed --dataset-version v1 \
  --as-user ci-evaluator --baseline evaluation/baseline.json --fail-on-regression
```

- CI runs v1, then v2, against the **committed** baselines (`evaluation/baseline.json` and
  `baseline-v2.json`), on the offline providers.
- A quality metric below its baseline, beyond `--tolerance`, fails the build.
- The gate knows which direction is "better" for each metric.
- Latency is reported but gated only with `--gate-latency`, because wall-clock time varies
  between runs.

### Current results

These are synthetic results, measured on a CPU laptop.

| v2 configuration | R@1 | MRR | Coverage | Abst. acc. | Citation acc. | p50 |
|---|---|---|---|---|---|---|
| hashing + lexical (offline default, the CI baseline) | 0.796 | 0.860 | 0.597 | 0.597 | 0.794 | 41 ms |
| bge-small + bge-reranker-base @ calibrated 0.02 | 0.857 | 0.926 | 0.544 | 0.614 | 0.860 | ~6 s |
| **bge-small + MiniLM-L6** (recommended) | **0.980** | **1.000** | 0.491 | 0.561 | 0.875 | 644 ms |

**What the numbers say.**

- A cross-encoder helps retrieval a lot, and the small MiniLM model is both the best ranker and
  about 10× faster.
- **Coverage is limited by the extractive answerer, not by retrieval.** It quotes only sentences
  that share the question's words, so it declines paraphrases even when the right passage was
  retrieved.
- A generative answerer is the component designed to fix that. It has not been benchmarked here.

Details, including the calibration method, are in
[docs/evaluation/README.md](docs/evaluation/README.md).

### Extraction benchmark

`python manage.py benchmark_extraction <folder> --approach pdf_text --approach ocr --json out.json`
compares the extraction approaches on your files. It reports pages, characters, time, memory and
failure rate, plus similarity and word accuracy wherever a same-stem `.txt` ground truth exists.

---

## 18. Observability

| Signal | Where |
|---|---|
| **Logs** | Structured JSON on stdout, with content redaction enforced at the formatter ([§11](#logging)). Every request has an `X-Request-ID` |
| **Metrics** | Prometheus at `/metrics` on each backend replica (`:8000`) and each worker container (`:9100`). Multiprocess-aggregated across gunicorn and Celery children |
| **Alerts** | `infrastructure/prometheus/alerts.yml`: 11 rules. No delivery is configured (no Alertmanager) |
| **Dashboards** | Grafana dashboard **AI Document Intelligence → adi-overview**, provisioned as code. Rows: API, Workers, Documents, RAG, LLM, Quality, Security |
| **Tracing** | OpenTelemetry, opt-in: build with `INSTALL_OTEL=true` and set `OTEL_EXPORTER_OTLP_ENDPOINT`. Django, Celery and psycopg are auto-instrumented; `rag.ask` and `search.hybrid` spans carry counts and timings, never text. Local viewer: `docker compose --profile tracing up -d jaeger` (UI on `127.0.0.1:16686`) |

### Key metrics

All labels are bounded vocabularies.

| Area | Metrics |
|---|---|
| HTTP | `adi_http_requests_total`, `adi_http_request_duration_seconds`, `adi_throttled_requests_total` |
| Pipeline | `adi_pipeline_jobs_total`, `adi_pipeline_stages_total`, `adi_pipeline_stage_duration_seconds`, `adi_pipeline_failures_total{error_code}`, `adi_pages_processed_total`, `adi_documents_indexed_total`, `adi_queue_depth{queue}` (read from Redis `LLEN`) |
| RAG | `adi_rag_requests_total`, `adi_rag_duration_seconds`, `adi_rag_stage_duration_seconds{stage}`, `adi_rag_abstentions_total{reason}`, `adi_rag_groundedness`, `adi_rag_candidates`, `adi_rag_passages_used`, `adi_rerank_degraded_total` |
| LLM | `adi_llm_requests_total`, `adi_llm_duration_seconds`, `adi_llm_tokens_total`, `adi_llm_characters_total`, `adi_llm_fallbacks_total{answerer,reason}` |
| Security | `adi_auth_failures_total`, `adi_authz_denials_total`, `adi_malware_verdicts_total`, `adi_suspicious_inputs_total`, `adi_upload_rejections_total` |
| Quality | `adi_answer_feedback_total`, `adi_evaluation_metric` |

### Alert rules

Thresholds are pilot starting points.

| Group | Alerts |
|---|---|
| Availability | `TargetDown`, `HighApiErrorRate` (5xx > 5%), `SlowApiP95` (> 2 s, excluding chat) |
| Processing | `IngestQueueBacklog` (> 100 for 15 min), `PipelineFailureRate` (> 20%), `MalwareDetected` |
| AI quality | `HighAbstentionRate` (> 80%), `RerankerDegraded`, `LlmFallbacks` |
| Security | `AuthenticationFailureBurst`, `PromptInjectionAttempts` |

---

## 19. Production deployment

The production stack is one host running Docker Compose with two files merged. The overlay is
never loaded automatically:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Services

| Service | Role |
|---|---|
| `nginx` | Edge image built from `infrastructure/nginx/Dockerfile`. Its build runs the frontend typecheck and tests. Serves TLS, the SPA, the API proxy (`least_conn` across replicas, re-resolved every 10 s), rate limits and security headers. **The only service published: ports 80 and 443** |
| `migrate` | One-shot `migrate`; `backend` and the workers start only after it succeeds |
| `backend` | gunicorn + Django (3 workers × 2 threads). Warms models before reporting healthy |
| `worker` | Celery, queue `ingest`, concurrency 2, `--max-tasks-per-child=100` |
| `worker-index` | Celery, queue `index`, concurrency 6 |
| `db` | `pgvector/pgvector:pg16`, not published |
| `redis` | `redis:7-alpine` with AOF persistence, not published, no password |
| `certbot` | Renews certificates every 12 h. nginx reloads every 6 h to pick them up |
| `prometheus`, `grafana` | Metrics. Grafana is on **127.0.0.1:3000 only**; reach it with `ssh -L 3000:localhost:3000 <host>` |

How the images are built:

- Application containers run as the unprivileged `app` user (uid 10001).
- The image is the artifact. There is no source bind mount and no compiler.
- `.dockerignore` keeps media, database files, `.env` and virtualenvs out of the image.
- Every Python install uses `backend/constraints.txt`, so a rebuild months later installs the
  same versions.

### First boot

1. **Fill in `.env`** with the [required variables](#required-in-production).
2. **Run the deployment check:** `python infrastructure/check_deployment_config.py`. It asserts
   the *merged* compose configuration:
   - no writable source mount;
   - only nginx publishes ports;
   - `DJANGO_DEBUG=False`;
   - non-root users;
   - tmpfs mounts with an owner;
   - the edge is the image with the frontend.

   Compose merges lists across files, which has silently undone production guarantees twice
   before. That is why this check exists.
3. **Make `./backups` writable** by uid 10001: `install -d -o 10001 -g 10001 backups`.
4. **Get a TLS certificate.**
   `NGINX_SERVER_NAME=docs.example.edu CERTBOT_EMAIL=ops@example.edu STAGING=1 sh infrastructure/init-letsencrypt.sh`.
   Run it with `STAGING=1` first, then without. It has **not yet been exercised against a real
   domain**. Behind an institutional load balancer, put its certificate in the `letsencrypt`
   volume instead.
5. **Download the models** with `… run --rm backend python manage.py warm_models`, then set
   `HF_HUB_OFFLINE=1`.
6. **Create the first administrator** with `… run --rm backend python manage.py createsuperuser`.
   Then create departments, then accounts.
7. **Add the cron jobs** from [§15](#15-management-commands), and copy backups off the host.

### Upgrades

```bash
git pull
python infrastructure/check_deployment_config.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend python manage.py backup --label pre-upgrade
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

- `--no-deps` matters. Without it, `migrate` would run *before* the "pre-upgrade" backup is taken.
- A change of embedding model needs `reindex` afterwards. It runs on the `index` queue, beside
  live traffic.

### Scaling

`--scale backend=3` and `--scale worker=2` work without editing anything: nginx and Prometheus
discover replicas through Docker DNS. This is all **one host**. Multiple hosts need:

- object storage;
- shared PostgreSQL and Redis (with a Redis password);
- an external load balancer.

None of that is provided or verified; see
[docs/release/multi-host-validation.md](docs/release/multi-host-validation.md).

### Object storage

Build with `INSTALL_S3=true` and set `AWS_STORAGE_BUCKET_NAME` (plus `AWS_S3_ENDPOINT_URL` for
MinIO). The pipeline is storage-agnostic: `documents/storage.py` streams each object to a temp
file once per job and always deletes it. This has been contract-tested with a stub but **not
verified against a real bucket**.

### Backups and restore

```bash
python manage.py backup --prune                          # verified pg_dump + manifest (sha256, migration state)
python manage.py backup --include-media --label pre-upgrade
python manage.py restore /backups/db-<stamp>.dump        # reports only
python manage.py restore /backups/db-<stamp>.dump --yes  # replaces the tables
```

- **Restore refuses** a dump whose checksum does not match its manifest.
- It reports any app whose schema is ahead of or behind the code.
- **Rehearsed locally.** Backup, restore into an empty database, and a working search on the
  restored copy. Rollback across a migration was rehearsed with the runbook's exact commands.
- **Recovery targets.** The RPO is the backup interval. The RTO has not been measured at
  institutional scale.

The step-by-step deployment to a real server, the TLS test plan and the rollback procedure are in
[docs/release/staging-runbook.md](docs/release/staging-runbook.md).

---

## 20. Operations and troubleshooting

| Probe | Use |
|---|---|
| `GET /health/` | Liveness: restart on failure |
| `GET /ready/` | Readiness: take the instance out of rotation on 503; never restart on it |
| **Administration → System status** | A human's first look: dependencies, storage, queue depths, stalled runs, failures in the last 24 h, the AI configuration |

### Common situations

| Symptom | Cause, and what to do |
|---|---|
| Uploads stay *Queued* | The worker is down (`… up -d worker`), or an OCR-heavy batch is running. Watch `adi_queue_depth{queue="ingest"}`; add workers if CPU allows |
| Document stuck at *Processing* | The worker died mid-run. After 120 minutes it appears under Administration → Processing → Stalled. Run `recover_stalled_jobs --apply --requeue` (it should be in cron) |
| Reprocess returns 409 | A run is already queued or processing; follow it |
| The first search after a restart is slow (10–30 s) | The local model is loading. In production, run `warm_models` and set `HF_HUB_OFFLINE=1`; gunicorn and Celery warm the models at start |
| Search notes say "lexical results only" | The embedding provider could not load. `warm_models` reports why |
| Every answer says "reranking degraded" | The cross-encoder failed to load. Check the model cache and `adi_rerank_degraded_total` |
| Answers are "Quoted from sources" despite a configured LLM | The generative answerer fell back: a key, quota or SDK problem. See `adi_llm_fallbacks_total{reason}` and the note on the answer |
| "Could not be grounded" | The guardrail is working: a generated figure was not in the cited passages. Details are in `Message.metadata.grounding_failures` |
| Chat takes 10–20 s on CPU | The `bge-reranker-base` cross-encoder. Switch to MiniLM (`RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2`) |
| Sign-in fails with 403 "CSRF" | No CSRF cookie, or a different origin. Check `CSRF_TRUSTED_ORIGINS`; Vite's proxy must not use `changeOrigin` |
| System admin upload says "not a registered department" | Create the department first (Administration → Departments) |
| Permission errors on media or backups in production | uid 10001 must own the volumes and `./backups`; see [docs/deployment/production.md](docs/deployment/production.md#before-the-first-boot) |
| Backend crash-loops after a reboot with a `PermissionError` under `/tmp/prometheus` | The stack was started from an old compose file. `… up -d` recreates the container with an owned tmpfs |
| A file is quarantined | Intended. Only a system admin can download it. If the scanner was wrong, upload a clean copy as a new version |
| A user account may be compromised | Deactivate it: this ends its sessions and revokes its tokens. Review Administration → Audit, filtered by that user |

More is in the [operations runbook](docs/operations/runbook.md) and
[troubleshooting](docs/troubleshooting/common-issues.md).

### Performance, measured

Measured on a 4-core laptop CPU with 3,000 documents ([details](docs/operations/performance.md)):

- **Accepting uploads:** 15 documents/s.
- **Processing:** 1.73 documents/s with two worker processes; median job 659 ms. Indexing (chunk +
  embed) is 83% of stage time. Text-only corpus, so OCR did no work.
- **Chat:** 13.8 s p50 with `bge-reranker-base`, of which 13.4 s is reranking. Hence the MiniLM
  recommendation.
- **Not measured:** concurrent users, OCR-heavy corpora, larger scale.

---

## 21. CI/CD

`.github/workflows/ci.yml` triggers on pushes to `main`/`master`, on pull requests, and manually.

```
lint ─┬─ tests ──┬─ ai-regression (default branch only) ─┐
      │          └─ e2e ◄── frontend                      ├─► build ─► deploy-staging ─► smoke ─► deploy-production
      └─ security ────────────────────────────────────────┘
```

| Job | What it runs |
|---|---|
| `lint` | `ruff check` + `ruff format --check` on `backend` and `ai` (ruff pinned to 0.16.8) |
| `tests` | `makemigrations --check`, the backend suite on pgvector + Redis service containers, the AI workspace suite |
| `frontend` | `npm ci`, typecheck, Vitest, production build, `npm audit` (advisory) |
| `e2e` | The dev stack via compose on the offline baselines, `seed_e2e`, then Playwright with bundled Chromium |
| `security` | Asserts `.dockerignore` coverage, `check --deploy --fail-level WARNING`, bandit, `pip-audit` (advisory) |
| `ai-regression` | Seeds and evaluates v1 and v2 against the committed baselines with `--fail-on-regression` |
| `build` | Builds the backend and web images, then runs `check_deployment_config.py` |
| `deploy-staging`, `smoke`, `deploy-production` | Placeholders wired to GitHub environments. `smoke` probes `STAGING_URL` |

**The workflow has never run on GitHub**, because the repository has no remote. Every job's
commands have been run locally and pass. The first real run should be watched: environmental
failures, such as service networking or Playwright dependencies, are the likely ones. See
[docs/operations/ci.md](docs/operations/ci.md).

---

## 22. AI workspace

[`ai/`](ai/) is for offline training and evaluation of document classifiers. **It imports no
Django**, and the hand-off to the backend is a versioned model artifact:

```
ai/ (train + evaluate) ──► artifact: ai/classification/artifacts/<name>/v<version>/model.joblib
                                │
backend/documents/processing/classification/text_classifier.py  (inference adapter)
                                │
Django pipeline (CLASSIFICATION stage) ──► ClassificationResult
```

- **Text baseline** (`document-classifier` v0.1.0):
  - TF-IDF + logistic regression, trained on a balanced 500-per-class subset of
    [QS-OCR](https://github.com/Quicksign/ocrized-text-dataset) (Tesseract text of RVL-CDIP).
  - **Test accuracy 0.7185**, macro-F1 0.7276.
  - At its abstention threshold (0.30): 91.4% accuracy on the 51.7% of documents it answers.
  - Predicts in about 1 ms per document.
  - The labels are **RVL-CDIP business classes** (letter, memo, invoice, …), not institutional
    types. Treat it as a demonstration of the integration, not an institutional classifier.
- **Vision baseline** (ResNet-50 on Kaggle's free GPU). The submit → train → download path is
  proven, but **only a smoke artifact exists. It is not trained.**

```bash
export PYTHONPATH=.
python -m ai.classification.datasets.qs_ocr --per-class 500            # download (252 MB) + balanced subset
python -m ai.classification.datasets.integrity ai/classification/data/manifests/qs_ocr --sample-limit 0
python -m ai.classification.training.train_text                        # ~40 s on 4 cores
python -m ai.classification.training.submit_kaggle --smoke --dry-run   # vision path (needs KAGGLE_USERNAME/KAGGLE_KEY)
python -m unittest discover -s ai/classification/tests -t .
```

Datasets, model artifacts and runs are git-ignored. More detail is in
[ai/classification/README.md](ai/classification/README.md) and
[docs/data/datasets.md](docs/data/datasets.md).

---

## 23. Technology stack

| Layer | Choice (pinned version) |
|---|---|
| Language | Python 3.12 (`python:3.12-slim`); TypeScript 5.9 |
| Backend | Django 6.1, Django REST Framework 3.18, gunicorn 26 |
| Database | PostgreSQL 16 + pgvector (`pgvector/pgvector:pg16`, Python `pgvector` 0.5); full-text search with a GIN index; HNSW vector index |
| Queue and cache | Celery 5.6 on Redis 7 |
| Parsing and OCR | pypdf 6.19, python-docx 1.2, pdf2image + poppler, pytesseract + Tesseract, Pillow 12 |
| ML (optional) | scikit-learn 1.9 (classifier); sentence-transformers 6.1 on CPU PyTorch 2.14 (`bge-small-en-v1.5` embeddings; `bge-reranker-base` or `ms-marco-MiniLM-L6-v2` cross-encoders) |
| LLM providers (optional) | `anthropic` 1.11 (API and Bedrock), `openai` 3.24 (OpenAI-compatible: Gemini, Groq, OpenRouter, Cerebras, Ollama), boto3 |
| Frontend | React 19, React Router 7, Vite 6; Vitest 5 + Testing Library; Playwright 1.63 |
| Edge | nginx 1.27 (Alpine), Let's Encrypt via certbot |
| Observability | prometheus-client, Prometheus 2.54, Grafana 11.2, OpenTelemetry (optional), Jaeger (local) |
| Storage (optional) | django-storages + S3 / MinIO |
| Tooling | Docker + Compose, ruff 0.16.8, bandit, pip-audit, GitHub Actions |

**Not built, by decision:** separate FastAPI AI services and a Next.js frontend. The modular
monolith and a static Vite SPA meet every requirement measured so far.

---

## 24. Project history

60 commits, from 2026-09-10 to 2026-10-11:

| Dates | Theme |
|---|---|
| Sep 10 | **Classification groundwork** in `ai/`: dataset adapters, manifests, integrity checks, the Kaggle GPU vision path |
| Sep 13 | **The platform lands**: document AI, search, RAG, security, evaluation, observability, scale. Also the text classifier, a testing console, the production stack with TLS and CI, S3 storage, Prometheus and Grafana, the governed benchmark and regression gate, and generative answering (Claude, Bedrock) |
| Sep 19 | **Hygiene and gates**: a ruff baseline, upload admission limits, CI gate repairs, an immutable regression corpus, document discovery |
| Sep 30 | **Product build-out**: semantic providers, follow-ups, fact-level grounding, browser sessions, the administration API, **the React product frontend** (replacing the console), proxy-aware throttles, the production stack verified end to end |
| Oct 5–6 | **Release readiness**: review fixes, the v2 benchmark gate, performance at 3,000 documents, reranker calibration, every dependency pinned, a release audit with a staging runbook and a decision record |
| Oct 10 | **Second review sweep** across every layer: about 60 bugs fixed, each with a regression test. Shell scripts forced to LF |
| Oct 11 | **Specification completion**: the `/api/v1` prefix and idempotent uploads, OpenTelemetry with alert rules and a Grafana dashboard, collections, the traceability matrix. Then **streamed chat**, opt-in general-knowledge replies, and the Gemini fallback chain |

Every phase of the specification's §25 roadmap has been worked through:

| Phase | Name | State |
|---|---|---|
| 0 | Architecture and environment | ✅ |
| 1 | Identity: users, auth, roles | ✅ |
| 2 | Document platform: upload, versions, lifecycle | ✅ |
| 3 | Async processing: queue, workers, retries, progress | ✅ |
| 4 | Document AI: parsing, OCR, classification, extraction | ✅ The vision classifier is untrained |
| 5 | Search: chunking, embeddings, pgvector, hybrid retrieval | ✅ |
| 6 | RAG: reranking, context, LLM gateway, citations, abstention | ✅ |
| 7 | AI security: injection defence, permission-aware retrieval | ✅ The scanner is heuristic, not an antivirus |
| 8 | Evaluation: datasets, metrics, regression | ✅ Synthetic data only |
| 9 | Observability: metrics, logs, traces, AI telemetry | ✅ Tracing is opt-in |
| 10 | Production: deployment, HTTPS, backups, CI/CD | ✅ CI has never run on GitHub; no real server yet |
| 11 | Scale: load balancing, worker scaling, service extraction | ✅ Single host; service extraction deliberately not done |
| 12 | Advanced AI: multimodal RAG, tables, charts, agents | ✅ Tables, figure location and question planning. Nothing reads images |

The reasoning behind each of these phases, including what went wrong and what was measured, is in
[docs/architecture/design-history.md](docs/architecture/design-history.md).

---

## 25. Known limitations and open decisions

### Not built

- SSO, SAML, OIDC or MFA; self-service password reset (nothing sends email yet).
- An antivirus engine adapter (the `DOCUMENT_SCANNER` interface exists).
- Encryption at rest in the application.
- Organisation-level tenancy (the tenant is the department).
- Anything that reads images: multimodal RAG, chart understanding, figure descriptions.
- Multilingual support: both local models are English.
- Bounding boxes in citations (optional in the specification).
- Retention and pruning of audit rows and chat history.
- Alert delivery (Alertmanager).
- Multi-host deployment.

### Unverified

- CI on GitHub.
- A real server, Let's Encrypt and off-host backups.
- S3 or MinIO against a real bucket.
- Hosted answerers other than the Gemini preset.
- Behaviour under concurrent users.
- OCR quality on a scanned corpus.
- **Any AI quality figure on real institutional documents.**

### Small known quirks

- The Bedrock Titan embedder needs a migration away from the 384-dimension column before it will
  load.
- `reindex` re-embeds every extraction pass, superseded ones included. That is more work than
  necessary, but harmless.
- Request ids are not propagated into Celery task logs, despite a comment in
  `observability/context.py` saying they are.
- nginx does not set `server_tokens off`, so it reveals its version.
- With `MAX_DOCUMENT_PAGES` at 2000, a long fully scanned PDF can exceed the 900 s soft time limit.
- `frontend/README.md` and `ai/README.md` predate collections, streaming and the trained text
  classifier.

### Decisions that belong to the institution

These are recorded in
[docs/release/institutional-decisions.md](docs/release/institutional-decisions.md), with what the
code does in the meantime. The ones that block a pilot:

- **Identity:** how users sign in (local accounts, VPN or identity-aware proxy, or SSO), and who
  may reach `/django-admin/`.
- **People:** the first administrators and departments.
- **Data:** which documents enter the pilot, and whether any hosted AI provider may receive
  document text.
- **Language:** confirmation that the corpus is English.
- **Evaluation:** who writes the institutional evaluation questions, and the AI acceptance
  thresholds.
- **Security:** malware-scanning policy.
- **Operations:** where backups go and who holds the key, where alerts go and who responds, and
  the server hardware.

---

## 26. Working on this codebase

- **Read [§1's rules](#rules-the-system-does-not-break) first.** Code review leans on them. A
  change that makes the system guess, skip a permission check, or trust document text will not
  pass review.
- **Every fix comes with a regression test that fails without the fix.** Say so in the test's
  docstring when it is not obvious.
- **Configuration lives in `settings.py`, and new providers live behind the existing protocols**,
  selected by a dotted-path setting. Every new provider needs a visible degraded path.
- **Never claim what you have not measured.** In docs, label claims *verified* or *unverified*,
  with the date and how.
- **Dependencies.** Change a pin in `backend/constraints.txt`, rebuild, then run the full suite
  and both evaluation gates before committing.
- **Lint and format** with ruff 0.16.8 (rules in `pyproject.toml`). Migrations must be committed
  with the model change; CI checks with `makemigrations --check`.
- **Commits.** Conventional style: `feat(scope): …`, `fix(scope): …`, `docs: …`, `test: …`. The
  subject says what changed for a user or operator. The body explains why, and what was measured.
- **The development database is precious.** Use a disposable database for e2e runs, benchmarks
  and restore drills. Other sessions may share the checkout, so check `git status` before
  committing.
- **Don't edit a loaded evaluation dataset.** Create a new version.
- **After editing either compose file**, run `python infrastructure/check_deployment_config.py`.

---

## 27. Further reading

| Topic | Page |
|---|---|
| Documentation index, and one table of what is verified | [docs/README.md](docs/README.md) |
| Architecture and code map | [docs/architecture/overview.md](docs/architecture/overview.md) |
| Specification → code → tests | [docs/architecture/spec-traceability.md](docs/architecture/spec-traceability.md) |
| Why things are the way they are (the former README) | [docs/architecture/design-history.md](docs/architecture/design-history.md) |
| Local development and testing | [docs/development/setup.md](docs/development/setup.md), [docs/development/testing.md](docs/development/testing.md) |
| Every API endpoint | [docs/api/reference.md](docs/api/reference.md) |
| Security model | [docs/security/model.md](docs/security/model.md) |
| The AI pipeline | [docs/ai/pipeline.md](docs/ai/pipeline.md) |
| Evaluation methods and results | [docs/evaluation/README.md](docs/evaluation/README.md) |
| Datasets and their provenance | [docs/data/datasets.md](docs/data/datasets.md) |
| Production deployment | [docs/deployment/production.md](docs/deployment/production.md) |
| Operations runbook, CI, performance | [docs/operations/runbook.md](docs/operations/runbook.md), [docs/operations/ci.md](docs/operations/ci.md), [docs/operations/performance.md](docs/operations/performance.md) |
| Troubleshooting | [docs/troubleshooting/common-issues.md](docs/troubleshooting/common-issues.md) |
| Release readiness, staging runbook, AI release, institutional decisions, multi-host | [docs/release/](docs/release/RELEASE_READINESS.md) |
| Web application | [frontend/README.md](frontend/README.md) |
| Classifier training | [ai/classification/README.md](ai/classification/README.md) |
