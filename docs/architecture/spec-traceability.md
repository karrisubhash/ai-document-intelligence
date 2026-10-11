# Specification traceability

Maps every requirement in *AI Document Intelligence Platform — Enterprise Technical
Documentation v1.0* to where it is implemented and how it is verified. Paths are relative
to `backend/` unless they start with another top-level folder.

Status: **Done**. **Adapted** means implemented differently from the specification's wording,
for the reason given. **Decision** means the code is ready and the remaining step belongs to
the deploying institution ([institutional-decisions.md](../release/institutional-decisions.md)).
**Deferred** means out of the initial-release scope by the specification's own §4.

## Functional requirements (§5)

| ID | Requirement | Status | Implementation | Verified by |
|---|---|---|---|---|
| FR-01 | Authentication | Done | `accounts/api/` (session + CSRF for browsers, expiring tokens for scripts), `accounts/authentication.py`, `documents/throttling.py`, nginx login limits | `accounts/tests_auth.py` |
| FR-02 | Organizations | Adapted | The tenant is the **Department** (`accounts/models.py`); every document, collection, conversation and job is department-scoped | `documents/tests_security.py` |
| FR-03 | RBAC | Adapted | Institutional roles, see [Role mapping](#role-mapping). Rules in `documents/authorization.py`, `documents/permissions.py`, `accounts/permissions.py` | `accounts/tests_admin.py`, `documents/tests_security.py` |
| FR-04 | Document upload | Done | `POST /api/v1/documents/` (`documents/api/views.py`), 202 + async job, `Idempotency-Key` support (`documents/idempotency.py`) | `tests_uploads.py`, `tests_api_contract.py` |
| FR-05 | Validation | Done | Size, type, integrity at upload (`documents/uploads.py`) and the VALIDATION stage; malware scan stage (`processing/scanning/`) → QUARANTINED | `tests_uploads.py`, `tests_security.py` |
| FR-06 | Storage | Done | Originals in filesystem or S3-compatible storage (`documents/storage.py`, `requirements-s3.txt`); metadata, SHA-256 checksum and versions in PostgreSQL | `tests_object_storage.py`, `tests_storage.py` |
| FR-07 | Observable processing states | Done | `ProcessingJob` + `ProcessingEvent` per stage (`processing/pipeline.py`, `processing/stages/`); `GET /documents/{id}/processing/` | `tests_ingestion.py`, `tests_lifecycle.py` |
| FR-08 | OCR | Done | `processing/stages/ocr.py`, `processing/extractors/ocr.py` (Tesseract, preprocessing, `OCR_DPI`) | `tests_ingestion.py` |
| FR-09 | Classification | Done | `processing/stages/classification.py`; trained text classifier in `ai/classification/` (versioned artifact, metrics) | `tests_classification.py`, `tests_text_classifier.py`, `ai/classification/tests/` |
| FR-10 | Extraction | Done | `processing/field_extraction/` (schema-validated fields), tables and figures | `tests_field_extraction.py`, `tests_tables.py`, `tests_figures.py` |
| FR-11 | Chunking | Done | `processing/chunking/` (page-preserving, verified character offsets) | `tests_search.py` |
| FR-12 | Indexing | Done | `processing/embedding/` → `DocumentChunkEmbedding` (pgvector) + `search_vector` (tsvector); controlled re-index (`tasks_reindex.py`, `manage.py reindex`) | `tests_search.py`, `tests_scale.py` |
| FR-13 | Search | Done | Hybrid lexical + vector with fusion, permission filter first (`search/service.py`) | `tests_search.py` |
| FR-14 | RAG chat over collections | Done | `rag/engine.py`, `chat/views.py`, multi-part planning (`agents/`), follow-ups (`rag/followup.py`); **Collections** (`collections/`, `models/collection.py`) narrow search and chat | `tests_rag.py`, `tests_agents.py`, `tests_followup.py`, `tests_collections.py` |
| FR-15 | Citations | Done | §12.3 shape: document, page, chunk, text, character start/end (`search/service.py`, `rag/grounding.py`). Bounding boxes (optional in §12.3) are not produced | `tests_rag.py` |
| FR-16 | Abstention | Done / Adapted (chat, opt-in) | `rag/engine.py` `Abstention` reasons; relevance floor; grounding check. With `CHAT_GENERAL_KNOWLEDGE` on, chat replaces a clean abstention with a labelled, uncited general-knowledge reply (`rag/general.py`) - never after a grounding failure, never in search or evaluation | `tests_rag.py`, `tests_evaluation_gate.py` |
| FR-17 | Evaluation | Done | Offline: `evaluation/` benchmarks, `manage.py evaluate`, CI regression gate. Online: answer feedback, `adi_evaluation_metric`, groundedness histogram | `tests_evaluation.py`, `tests_evaluation_gate.py` |
| FR-18 | Audit | Done | `documents/audit.py`, `AuditLog` (document, auth, admin, collection actions); `GET /api/v1/admin/audit/` | `tests_security.py`, `accounts/tests_admin.py` |

## Non-functional requirements (§6)

| ID | Area | Status | Implementation |
|---|---|---|---|
| NFR-01 | Availability | Done | `/health/`, `/ready/`, restart policies, `manage.py recover_stalled_jobs`, graceful degradation of reranker and answerer |
| NFR-02 | Scalability | Done (single host) | Separate `ingest` and `index` queues and workers, gunicorn workers; multi-host steps in [multi-host-validation.md](../release/multi-host-validation.md) |
| NFR-03 | Performance | Done | Upload returns 202; all OCR, parsing and embedding run in Celery. Measurements in [performance.md](../operations/performance.md) |
| NFR-04 | Security | Done / Decision | [security/model.md](../security/model.md); remaining items (antivirus engine, HSTS, SSO/MFA, encryption at rest) are institutional decisions |
| NFR-05 | Reliability | Done | Retryable error classification with backoff (`tasks.py`), `acks_late`, idempotent stages, quarantine as a terminal state, idempotent uploads |
| NFR-06 | Observability | Done | Structured JSON logs with request ids (`observability/logging.py`, `middleware.py`); Prometheus metrics (`observability/metrics.py`); **OpenTelemetry tracing** (`observability/tracing.py`, opt-in); **alert rules** (`infrastructure/prometheus/alerts.yml`); **Grafana dashboard** (`infrastructure/grafana/dashboards/`) |
| NFR-07 | Maintainability | Done | Provider abstractions: answerers (`rag/llm.py`, `rag/claude.py`, `rag/openai_compat.py`, local extractive), rerankers (`rag/reranking.py`, local, Bedrock), embedding providers |
| NFR-08 | Testability | Done | Backend unit/API/integration/security suites, AI tests, frontend Vitest, Playwright end-to-end, all in CI (`.github/workflows/ci.yml`) |
| NFR-09 | Auditability | Done | Audit log; evaluation runs store model, prompt and pipeline versions |
| NFR-10 | Cost control | Done | `adi_llm_tokens_total`, `adi_llm_characters_total`, `adi_llm_requests_total` per model; hosted answerer off by default |

## Architecture and operations sections

| § | Topic | Status | Notes |
|---|---|---|---|
| 7 | Modular monolith with explicit boundaries | Done | Django apps `accounts` and `documents`, with sub-packages per domain (`processing`, `search`, `rag`, `chat`, `collections`, `evaluation`, `observability`, `operations`) |
| 9.1 / 10 | Upload flow, state machine, idempotency | Done | Statuses UPLOADED → QUEUED → PROCESSING → COMPLETED, FAILED, QUARANTINED, with stage events for each §10.1 step |
| 12.1 | Query rewrite | Done | Follow-up rewriting (`rag/followup.py`) and planning (`agents/`) |
| 13 | Prompt and document injection defences | Done | Retrieved text treated as data; suspected injections counted (`adi_suspicious_inputs_total`) |
| 15.2 | Core entities | Done | Organization → Department; DocumentPage → `PageContent`; Embedding → `DocumentChunkEmbedding`; AuditEvent → `AuditLog`; `Collection` |
| 16 | API: versioning, pagination, request ids, idempotency keys | Done | `/api/v1/` (alias `/api/`), page-number pagination, `X-Request-ID`, `Idempotency-Key` on uploads. [API reference](../api/reference.md) |
| 17 | Queue and workers, retry policy | Done | Celery + Redis; dead-letter path is QUARANTINED / FAILED with stage and error code recorded |
| 20 | Distributed tracing | Done (opt-in) | Build with `INSTALL_OTEL=true`, set `OTEL_EXPORTER_OTLP_ENDPOINT`; local viewer `docker compose --profile tracing up -d jaeger` |
| 21 | Docker, nginx, HTTPS, backups, CI/CD | Done / Decision | Compose production stack, Let's Encrypt, `manage.py backup/restore`, CI workflow. CI has not yet run on a hosted runner (no remote) |
| 22 | Scaling stages | Stage 1–2 | Stages 3–5 wait for measured need, as §22 requires |
| 26 | Next.js, FastAPI | Adapted | React + Vite (static build served by nginx; no server rendering needed). FastAPI not introduced: inference runs in the Celery workers and the API process, so a separate service would add a hop with no workload to justify it (§7.1) |
| 24 | Project structure | Adapted | AI pipeline code lives under `backend/documents/processing` and `backend/documents/rag`; top-level `ai/` holds classifier training |
| 28 | Future enhancements | Deferred | Multilingual, voice, comparison, multimodal RAG and the rest of §28 |

## Role mapping

| Specification | This platform | Difference |
|---|---|---|
| SUPER_ADMIN | SYSTEM_ADMIN (or Django superuser) | — |
| ORG_ADMIN | DEPARTMENT_ADMIN | Manages users, documents and collections of one department |
| MANAGER | FACULTY | Creates and curates collections; manages own documents |
| USER | STAFF | Uploads, searches and chats over authorized documents |
| VIEWER | STUDENT | Searches and chats. Unlike a strict VIEWER, may upload own documents |
