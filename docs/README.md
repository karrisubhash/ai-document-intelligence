# Documentation

Engineering documentation for the AI Document Intelligence Platform. Every page describes
what the code in this repository **does today**; anything that has not been exercised is
labelled as such. The top-level [README](../README.md) is the long-form project history and
design rationale; these pages are the working reference.

| Area | Page | What it answers |
|---|---|---|
| Architecture | [architecture/overview.md](architecture/overview.md) | Components, data flow, boundaries, and why it is one Django service |
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

## Verification status, in one place

| Claim | Status |
|---|---|
| Backend test suite (≈890 tests) passes against PostgreSQL + pgvector | **Verified** locally, repeatedly |
| Frontend typecheck, 32 unit/component tests, production build | **Verified** locally, and inside the web image build |
| Browser end-to-end suite against the live stack (real worker, real models) | **Verified** locally: 7/7 on the dev stack and 7/7 on the production stack over TLS, plus a CSP-violation probe |
| Backup, restore into an empty database, and a working search on the restored copy | **Verified** locally on a disposable database |
| Production compose stack boots behind nginx with TLS | **Verified locally** with a self-signed certificate: browser suite 7/7 over TLS, no CSP violations. **Real domain and Let's Encrypt: unverified** |
| GitHub Actions pipeline | **Never executed** — the repository has no remote. Job commands were run locally; see [operations/ci.md](operations/ci.md) |
| Hosted AI providers (Anthropic, Bedrock, OpenAI-compatible) | **Contract-tested with stubs; never called for real** — no credentials |
| Local semantic embeddings and cross-encoder reranking | **Verified** — run in the e2e stack and measured in [evaluation](evaluation/README.md) |
| Performance at institutional scale | **Measured at 3,000 documents** on a laptop CPU, one client: [operations/performance.md](operations/performance.md). Concurrent load, OCR-heavy corpora and larger corpora are **not measured** |
