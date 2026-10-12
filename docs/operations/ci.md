# CI/CD

`.github/workflows/ci.yml`:

```
lint ─┬─ tests ──┬─ ai-regression (default branch) ─┐
      │          └─ e2e ◄────── frontend ───────────┤   (frontend has no `needs`; it
      └─ security ──────────────────────────────────┤    starts beside lint)
                                         frontend ──┴─ build ─► deploy-staging ─► smoke ─► deploy-production
```

`build` - and so every deploy - needs **all** of them. It used to need only `tests`,
`security` and `frontend`, so a failing AI regression gate or browser suite ran beside a
deploy instead of stopping it.

| Job | Runs | Gate |
|---|---|---|
| `lint` | `ruff check` + `ruff format --check` (ruff pinned to 0.16.8, rules in `pyproject.toml`) | any finding |
| `tests` | migration check; the backend suite (run from `backend/`; from the repository root Django's discovery found none of it - `backend/` has no `__init__.py`) on PostgreSQL+pgvector and Redis service containers; the AI workspace suite | any failure |
| `frontend` | `npm ci`, typecheck, Vitest, production build; `npm audit` (advisory) | any failure |
| `e2e` | the dev stack via Docker Compose on the offline baselines, `seed_e2e`, Playwright (bundled Chromium) running every spec in `frontend/e2e/` (the CSP probe skips itself, since Vite sends no policy) | any failure; the results are uploaded as the `e2e-results` artifact and the backend and worker logs printed to the job log |
| `security` | `.dockerignore` must exclude media, database files, venvs and `.env`; `check --deploy --fail-level WARNING`; bandit; pip-audit (advisory) | any finding except the advisory audit |
| `ai-regression` | seeds the governed v1 corpus through the real pipeline and evaluates against `evaluation/baseline.json`, then seeds v2 on top and evaluates against `evaluation/baseline-v2.json`, both with `--fail-on-regression` (v1 is saturated; v2 is what moves) | a metric below baseline minus tolerance |
| `build` | the backend image and the web edge image (whose build runs the frontend checks again); `check_deployment_config.py` on the merged production compose (it supplies `.env.example` when no `.env` exists, so it runs on a fresh checkout) | build failure or a config violation |
| `deploy-staging`, `smoke`, `deploy-production` | placeholders wired to GitHub environments; smoke probes `STAGING_URL/health/` for a healthy status and requires a 401 or 403 on an anonymous API call | environment approvals |

## What has actually been executed

**The workflow has never run on GitHub Actions** — the repository has no remote. Each
job's commands have been executed locally, which proves the commands, not the YAML.
actionlint 1.7.7 (with shellcheck) finds no errors in the YAML, only two style warnings
about an unused loop counter.

Latest local run: 2026-10-12, on `release/pilot` at 9f2772b, each job's steps from a clean
`git archive` of the commit, with the workflow's environment values, against a disposable
pgvector/pg16 and Redis 7 ([release readiness](../release/RELEASE_READINESS.md#locally-verified)):

| Job | Executed locally | Result |
|---|---|---|
| lint | yes | clean (ruff 0.16.8) |
| tests | yes, in a Python 3.12 container with only the job's apt and pip installs | migration check clean; 1,142 backend tests OK, 1 skipped (it needs the optional OpenTelemetry layer, and passes where that is installed); AI suite 88 OK. Also 1,142 OK in the release image with the source mounted read-only |
| frontend | yes (Node 22) | typecheck clean; 61/61 under load; build; `npm audit`: 0 |
| e2e | yes, **with the job's own steps** for the first time: `.env` from `.env.example`, `docker compose up -d --build`, `seed_e2e`, the Vite dev server, Playwright's bundled Chromium; only the ports moved (18000, 15173) | 7 passed, 1 skipped by design (the CSP probe). Before a827d55 the same steps failed twice: Vite reloaded the page whenever Playwright wrote a trace into `e2e/.results` |
| security | yes | `.dockerignore` gate, `check --deploy`, bandit and pip-audit all pass. On a Windows checkout the `.dockerignore` gate fails falsely: `core.autocrlf` writes the file with CRLF and `grep -x` then matches no line. CI's checkout is LF |
| ai-regression | yes, as the job runs it | v1 and v2: "no change" against their baselines; see [evaluation/README.md](../evaluation/README.md#the-regression-gate) |
| build | yes | backend and web images built with BuildKit; `check_deployment_config.py` passes |
| deploy / smoke | no | need a staging host and credentials |

Earlier runs: 2026-10-05/06 (every job's commands; the e2e configuration through
`docker compose run` rather than the job's own `.env` step) and 2026-10-10 on bb34379
(every gate, Playwright 7 passed with the CSP probe skipped by design).

The first real run should be watched. Likely failure points are environmental:
service-container networking for the e2e compose stack, Playwright's system dependencies
(`--with-deps` on the runner, not exercised locally), and timing on a slower runner. Two
timing defects the local runs exposed are fixed: the dev-server reload under the browser
suite (a827d55) and frontend tests that gave up after 1 s on a loaded machine (3ebf20d).
