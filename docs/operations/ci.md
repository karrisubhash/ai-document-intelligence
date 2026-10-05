# CI/CD

`.github/workflows/ci.yml`:

```
lint ─┬─ tests ─────┬─ ai-regression (default branch) ─┐
      │             ├─ e2e ◄── frontend ───────────────┤
      ├─ security ──┼──────────────────────────────────┤
      └─ frontend ──┴──────────────────────────────────┴─ build ─► deploy-staging ─► smoke ─► deploy-production
```

`build` - and so every deploy - needs **all** of them. It used to need only `tests`,
`security` and `frontend`, so a failing AI regression gate or browser suite ran beside a
deploy instead of stopping it.

| Job | Runs | Gate |
|---|---|---|
| `lint` | `ruff check` + `ruff format --check` (ruff pinned to 0.16.8, rules in `pyproject.toml`) | any finding |
| `tests` | migration check; the backend suite (run from `backend/`; from the repository root Django's discovery found none of it - `backend/` has no `__init__.py`) on PostgreSQL+pgvector and Redis service containers; the AI workspace suite | any failure |
| `frontend` | `npm ci`, typecheck, Vitest, production build; `npm audit` (advisory) | any failure |
| `e2e` | the dev stack via Docker Compose on the offline baselines, `seed_e2e`, Playwright (bundled Chromium) running `frontend/e2e/platform.spec.ts` | any failure; results and stack logs uploaded on failure |
| `security` | `.dockerignore` must exclude media, database files, venvs and `.env`; `check --deploy --fail-level WARNING`; bandit; pip-audit (advisory) | any finding except the advisory audit |
| `ai-regression` | seeds the governed v1 corpus through the real pipeline, evaluates against `evaluation/baseline.json` with `--fail-on-regression` | a metric below baseline minus tolerance |
| `build` | the backend image and the web edge image (whose build runs the frontend checks again); `check_deployment_config.py` on the merged production compose (it supplies `.env.example` when no `.env` exists, so it runs on a fresh checkout) | build failure or a config violation |
| `deploy-staging`, `smoke`, `deploy-production` | placeholders wired to GitHub environments; smoke probes `STAGING_URL` for health and for a 401 on an anonymous API call | environment approvals |

## What has actually been executed

**The workflow has never run on GitHub Actions** — the repository has no remote. Each
job's commands have been executed locally, which proves the commands, not the YAML:

| Job | Executed locally | Notes |
|---|---|---|
| lint | yes | clean |
| tests | yes, in the backend image against pgvector/pg16 and Redis 7 | 960 tests, 0 failures |
| frontend | yes | 44 tests; build and typecheck clean |
| e2e | yes, in an equivalent form | **CI's own configuration** (2026-10-05): hashing embeddings, lexical reranking, heuristic scanner, rule-based extraction, no classifier, Playwright's bundled Chromium, the Vite dev server proxied to the backend: 7/7, with the CSP probe skipped as designed (Vite sends no policy). Also 7/7 plus the CSP probe against the production stack over TLS with the full local-model configuration. The stack was started with `docker compose run` and environment overrides rather than by the job's `.env` step, so the job's compose wiring itself is what a first real run proves |
| security | partially | `check_deployment_config.py` and the `.dockerignore` check run; `check --deploy` and bandit are as before |
| ai-regression | yes, as the job runs it (2026-10-05) | fresh database, corpus seeded through the pipeline, `--fail-on-regression` against `evaluation/baseline.json`: passes; see [evaluation/README.md](../evaluation/README.md#the-regression-gate) |
| build | yes | backend and web images built with Docker BuildKit |
| deploy / smoke | no | need a staging host and credentials |

The first real run should be watched; the likely failure points are environmental
(service-container networking for the e2e compose stack, Playwright's system dependencies)
rather than in the commands themselves.
