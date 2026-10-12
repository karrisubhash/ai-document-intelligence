# Documentation

Engineering documentation for the AI Document Intelligence Platform. Every page describes
what the code in this repository **does today**; anything that has not been exercised is
labelled as such. The top-level [README](../README.md) is the long-form project history and
design rationale; these pages are the working reference.

| Area | Page | What it answers |
|---|---|---|
| Architecture | [architecture/overview.md](architecture/overview.md) | Components, data flow, boundaries, and why it is one Django service |
| Spec traceability | [architecture/spec-traceability.md](architecture/spec-traceability.md) | Every FR/NFR of the specification mapped to code and tests; adaptations and role mapping |
| Development | [development/setup.md](development/setup.md) | Running the stack locally |
| | [development/testing.md](development/testing.md) | The four test layers and how to run each, including the browser suite |
| API | [api/reference.md](api/reference.md) | Every endpoint, who may call it, and what it returns |
| Deployment | [deployment/production.md](deployment/production.md) | The production stack, TLS, images, first boot, configuration |
| Operations | [operations/runbook.md](operations/runbook.md) | Health, readiness, stalled jobs, re-indexing, backups and restore |
| | [operations/ci.md](operations/ci.md) | The CI pipeline, and exactly what has and has not been executed |
| | [operations/performance.md](operations/performance.md) | Measured ingestion throughput, latency, query plans and resource use at 3,000 documents |
| Security | [security/model.md](security/model.md) | Identity, authorization, sessions, uploads, AI security, audit, known gaps |
| AI | [ai/pipeline.md](ai/pipeline.md) | Retrieval, reranking, answering, grounding, providers, follow-ups |
| Evaluation | [evaluation/README.md](evaluation/README.md) | Datasets, metrics, the regression gate, measured results |
| Data | [data/datasets.md](data/datasets.md) | Provenance and intended use of every dataset in the repository |
| Troubleshooting | [troubleshooting/common-issues.md](troubleshooting/common-issues.md) | Symptoms, causes and fixes seen in practice |
| Release | [release/RELEASE_READINESS.md](release/RELEASE_READINESS.md) | What is done, what is verified, what blocks staging and production, and the go-live checklist |
| | [release/staging-runbook.md](release/staging-runbook.md) | Deploying to a real server step by step, the TLS test plan, rollback and smoke tests |
| | [release/ai-release.md](release/ai-release.md) | What may be claimed about the AI, the reranker decision, hosted-answerer activation, the real-data plan |
| | [release/institutional-decisions.md](release/institutional-decisions.md) | The decisions only the institution can make, with what the code does meanwhile |
| | [release/multi-host-validation.md](release/multi-host-validation.md) | Validating S3/MinIO and a second host before relying on either |

## Verification status, in one place

| Claim | Status |
|---|---|
| Backend test suite (1,142 tests) passes against PostgreSQL + pgvector | **Verified** locally, repeatedly; last on 2026-10-12 on `release/pilot`, in the pinned release image and in CI's environment ([release readiness](release/RELEASE_READINESS.md#locally-verified)) |
| Frontend typecheck, 61 unit/component tests, production build | **Verified** locally, and inside the web image build (2026-10-12) |
| Browser end-to-end suite against the live stack (real worker, real models) | **Verified** locally: with CI's e2e job's own steps, 7/7 with the CSP probe skipped by design (2026-10-12, `release/pilot`); on the production stack over TLS, 7/7 plus the CSP-violation probe (2026-10-10 and 2026-10-06, with the recommended pilot configuration) |
| Backup, restore into an empty database, and a working search on the restored copy | **Verified** locally on a disposable database; the staging runbook's backup, restore and rollback steps rehearsed verbatim on 2026-10-06. Off-host backups and production disaster recovery: **unverified** |
| Production compose stack boots behind nginx with TLS | **Verified locally** with a self-signed certificate: browser suite 7/7 over TLS, no CSP violations. **Real domain and Let's Encrypt: unverified** |
| GitHub Actions pipeline | **Never executed** — the repository has no remote. Job commands were run locally; see [operations/ci.md](operations/ci.md) |
| Hosted AI providers (Anthropic, Bedrock, OpenAI-compatible) | **Contract-tested with stubs.** Only the Gemini preset has been called for real: in development, against Google's free tier, with a developer's key (2026-10-11). No deployment has called any provider, and no evaluation has run through one |
| Local semantic embeddings and cross-encoder reranking | **Verified** — run in the e2e stack and measured in [evaluation](evaluation/README.md) |
| Performance at institutional scale | **Measured at 3,000 documents** on a laptop CPU, one client: [operations/performance.md](operations/performance.md). Concurrent load, OCR-heavy corpora and larger corpora are **not measured** |
