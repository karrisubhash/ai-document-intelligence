# Release readiness

The state of the system as it leaves the development laptop: what is built, what has been
proved, and what still stands between it and a staging server, a production server and a
pilot. Written 2026-10-06 for branch `platform-completion`, after a final release audit.
That audit comprised:

- three independent code reviews: security, the LLM gateway, and storage with
  multi-host operation;
- an image build from pinned dependencies;
- the full test suite;
- a release-candidate run of the production stack.

**Re-verified 2026-10-12 on branch `release/pilot`**, the branch to push for the first CI
run. It is `platform-completion`, which had moved 22 commits past the audit, with the
unmerged `release-review-fixes` merged in, two build fixes and two test-harness fixes
(fixes 15–18). The re-verification ran every
CI job's commands locally and re-checked the repository and the local images. It was not a
new code review. [Since the 2026-10-06 audit](#since-the-2026-10-06-audit) lists what
changed; [Locally verified](#locally-verified) lists what ran.

Every claim says how it was verified. "Locally" means on the development laptop, never on
a server. The supporting pages are:

| Page | Use it for |
|---|---|
| [staging-runbook.md](staging-runbook.md) | the 19-step deployment, the TLS test plan, rollback, smoke tests |
| [ai-release.md](ai-release.md) | AI claims, the reranker decision, hosted-answerer activation, the real-data plan |
| [institutional-decisions.md](institutional-decisions.md) | every decision that belongs to the institution |
| [multi-host-validation.md](multi-host-validation.md) | object storage and a second host |
| [operations/runbook.md](../operations/runbook.md) | day-to-day operation and failure response |

**How items are classified.** Each item below is one of:

- **A** code complete;
- **B** locally verified;
- **C** requires external infrastructure;
- **D** requires an institutional decision;
- **E** requires real data;
- **F** optional future improvement.

The sections follow that order.

## SYSTEM STATUS

| Area | Status | In one line |
|---|---|---|
| Code | **COMPLETE** for a single-host pilot | Nothing is missing for the pilot scope. Multi-host operation, SSO and a hosted answerer are outside it |
| Local verification | **COMPLETE**, re-run 2026-10-12 | 1,142 backend tests, both in the release image and in CI's environment; 88 AI-suite and 61 frontend tests; both AI gates at their baselines; the migration graph along every upgrade path; the e2e job as CI runs it, 7 passed and 1 skipped by design. Backup, restore and rollback last rehearsed 2026-10-06; browser suite 8/8 through nginx over TLS last run 2026-10-10 |
| CI | **NOT YET VERIFIED** | There is no GitHub repository, so the workflow has never run. actionlint finds no errors in it, and every job's commands pass locally (2026-10-12) |
| Staging | **BLOCKED** | It needs a server, a hostname and DNS, and the institution's before-staging decisions. No code change is required |
| Production | **BLOCKED** | It needs staging to pass, plus the before-pilot decisions |
| AI | **NEEDS REAL DATA** | The engineering is ready for a pilot: grounded, permission-filtered, and it declines rather than guesses. Quality on the institution's documents is unmeasured |
| Security | **No known release blocker** | 2 blocking defects found by the audit, 3 more by an independent review of its fixes, and about 60 defects by a file-by-file review (2026-10-10), each fixed with a test. Known gaps are documented; 7 security decisions belong to the institution |
| Performance | **Measured on one laptop only** | 3,003 documents, one client. Concurrency, scanned corpora and larger scale are unknown |

## CODE COMPLETE

**(A) Built.** The pieces are:

- the processing pipeline: ingestion with magic-byte sniffing, validation, malware
  scanning before any parser, text extraction, OCR, layout and tables, classification,
  field extraction, chunking and embedding;
- hybrid search: full text plus pgvector, fused, permission-filtered before retrieval;
- reranking (lexical or a local cross-encoder);
- answering: extractive by default, or Claude or an OpenAI-compatible model, with
  fact-level grounding and abstention;
- chat with follow-ups and question planning;
- department isolation enforced in the backend;
- a full audit trail;
- the administration screens;
- Prometheus metrics and structured logs with content redacted;
- backup and restore commands;
- the production compose stack: nginx with TLS, certbot renewal, a migration gate, and
  unprivileged containers;
- the deployment configuration check;
- the CI workflow;
- the evaluation framework, with two regression gates;
- added after the audit (2026-10-11): the `/api/v1/` prefix (with `/api/` kept as an alias)
  and idempotent uploads; department-scoped document collections; streamed chat answers
  and opt-in general-knowledge replies (decision D8); opt-in OpenTelemetry tracing, 13
  Prometheus alert rules and a Grafana dashboard.

**Fixed by this audit.** Each code fix has a test that fails without it.

| # | Defect | Consequence | Fix |
|---|---|---|---|
| 1 | Production dependencies were unpinned (`>=`) | a rebuild installed whatever was newest, so staging and production could run untested code | `backend/constraints.txt` pins all 90 packages of the tested image (111 since the OpenTelemetry layer and fix 16). Every install, in the image and in CI, uses it. Verified: the rebuilt image's packages are identical to the file |
| 2 | No image installed the S3 packages, and the S3 boto3 pin conflicted with the LLM layer | setting `AWS_STORAGE_BUCKET_NAME` broke every upload | optional `INSTALL_S3` build layer, versions from the constraints file |
| 3 | **Quarantine could be lifted by reprocessing** | the download gate read only the newest job; a reprocessed infected file was downloadable by the whole department until the rescan | the verdict persists until a later run completes; reprocess and retry of a quarantined file are refused. 3 tests |
| 4 | **Deleting a document erased the subject of its audit trail** | the delete row, and every earlier view and download, lost their document | each audit row keeps the document's id and department (migration 0017, with backfill); the delete row keeps the title; still withheld from other departments. 5 tests |
| 5 | The `.env.example` placeholder secret key passed the startup guard | a deployment copied from the example ran with a public key | the shipped placeholders are refused when DEBUG is off. 3 tests |
| 6 | The image's pip (25.0.1) had 6 published advisories | flagged by any image scanner | upgraded to 26.2.1. `pip-audit` of the image: no known vulnerabilities |
| 7 | Generated benchmark files from the laptop were copied into the image | the image depended on local, untracked files | excluded in `.dockerignore` |
| 8 | `requirements-lock.txt` was stale and missing 5 packages | the README's no-Docker setup failed at `migrate` | removed; the README installs with the constraints file |
| 9 | `frontend/.env.example` pointed the API at another origin | following it made sign-in impossible | rewritten to use the dev-server proxy |
| 10 | A test class wrote uploads into the real `backend/media` | test files leaked into the working tree | uses the temporary media directory like every other class |
| 11 | Documentation overclaimed | it said macros and PDF scripts were quarantined (they are only flagged), promised email (nothing sends mail), and said `--scale` spans machines (it does not) | corrected |
| 12 | A redelivered quarantined job could run again (found by an independent review of fix 3) | under `acks_late`, a worker dying after recording the verdict gets its message back; the rerun cleared the code the quarantine is read from | a quarantined job is terminal. Test |
| 13 | Migration 0017's new column had no database default | during an upgrade the previous release, still serving, could not write any audit row: a 500 on every audited request | migration 0018 adds a database default. Test |
| 14 | The department-rename guard ignored the department names on deleted documents' audit rows | renaming onto a spelling that survived only there handed that trail, titles included, to the renamed department | the guard checks them. Test |
| 15 | The gold manifest that `ingest_corpus` writes (`evaluation/corpus_manifest.json`, git-ignored) was not excluded from the build context (found 2026-10-12) | an image built on a machine that had run `ingest_corpus` carried its local document ids and dataset labels | excluded in `.dockerignore` (b77a9ca). Found by listing what a build from the development checkout copies: 262 files, the manifest the only untracked one |
| 16 | `setuptools`, which torch installs, was the one package in the image without a pin (found 2026-10-12) | a rebuild could install a different setuptools from the tested one | pinned at the tested 84.0.0 (f451ccb). Verified: every package of the rebuilt image matches the file |
| 17 | The Vite dev server watched `frontend/e2e/.results`, where Playwright writes its traces during the browser suite (found 2026-10-12) | a trace's `.html` resource made Vite reload the page under the test: the upload dialog vanished, the OCR test timed out, and the five serial tests after it never ran. CI runs Vite and Playwright in the same directory, so the first CI run would have failed this way; earlier local runs served Vite from another copy of the source | the dev server ignores that directory (a827d55). The e2e job failed twice before the fix and passed after it, with no reload |
| 18 | Frontend tests waited only Testing Library's default 1 s for async elements (found 2026-10-12) | on a loaded machine, ChatPage and CollectionsPage tests failed at 1.2–1.7 s: once in the web image build, twice on the host | a 5 s limit (3ebf20d). 57/57 three times in a row under the same load |

The same review raised two more findings. Both were fixed on `platform-completion` by the
file-by-file review of 2026-10-10, each with a test:

- the audit list's `?document=` and `?user=` filters returned 500 for digits such as `²`:
  they answer 400 (8ba7ed4);
- a version quarantined after it once completed kept its extracted text searchable:
  quarantine now clears the version's current content, and search reads only the current
  content's chunks (9270520); the content, chunk, table, figure and source endpoints no
  longer fall back to the condemned pass (8ba7ed4).

Fixes 12–14 were merged into `release/pilot` on 2026-10-12 (3e02d9d). Both branches had
added a migration numbered 0018; see [Migrations](#migrations).

### Since the 2026-10-06 audit

The audit's three code reviews covered the code as of c4ab67a. After it:

| Commits | What | How it was checked |
|---|---|---|
| 8ba7ed4 … bb34379 (8) | A file-by-file review of the whole code base: about 60 defects and 7 tests that could not fail, across the API, security, processing, RAG, evaluation, operations, frontend and AI code | each fix has a regression test; every CI gate passed on bb34379 (2026-10-10) |
| e34d1c7 | `.gitattributes` keeps shell scripts LF: on a Windows checkout a CRLF entrypoint made the edge image exit 127 | the edge image built from a fresh Windows clone started and served TLS |
| d4f9913 … e5eb275 (4) | The specification's remaining gaps: API versioning and idempotent uploads; OpenTelemetry, alert rules and a dashboard; collections; a traceability matrix ([spec-traceability](../architecture/spec-traceability.md)) | tests, then the independent review below |
| 016cea6 … 3a34936 (4) | Streamed chat, opt-in general-knowledge replies, the Gemini preset's model fallback chain, and fixes found by using the chat in a browser | tests, a browser check against the development stack, then the review below |
| 7a0efaf … 6641009 (4), merged in 9f2772b | An independent review of the eight commits above. Among its fixes: a department rename now carries collections; an idempotency key is matched to the file's content, not its name and size, and a claim expires instead of living 24 hours; search text no longer leaves in trace URLs; alert rules that could not fire, or fired on every page load; the Gemini chain drops a model that rejects its settings | each fix has a test; the merge's one conflict (the rename guard) keeps both sides' checks |
| 3e02d9d | Merge of `release-review-fixes`: fixes 12–14 and the chat latency measured through the edge | the merge, the merge migration and the whole suite (below) |
| d8edd3a, merged in 76ac4b6 | `.env.example` documents `INSTALL_LOCAL=false` (compose's value when it is absent); the local embedder's and reranker's import errors give the Docker fix | the whole suite (below) |
| b77a9ca, f451ccb | Fixes 15 and 16 | rebuilt images |
| a827d55, 3ebf20d | Fixes 17 and 18 (two frontend files) | the e2e job and the frontend suite (below) |

## LOCALLY VERIFIED

**(B)** All of this ran on the development laptop: an i5-8350U, Docker Desktop, an 8-CPU
and 7.7 GiB VM.

**On `release/pilot`, 2026-10-12.** Each CI job's steps ran from a clean `git archive` of
the commit, against a disposable pgvector/pg16 and Redis 7, with the workflow's own
environment values. Every job below ran on 9f2772b, the last commit that changes code; the
commit recording this page changes only documentation. They had also passed on the
branch's earlier commits (f451ccb, 3ebf20d), before the independent review's fixes were
merged.

| What | Result |
|---|---|
| `lint`: ruff 0.16.8, check and format | clean; 242 files formatted |
| `tests`, in CI's environment (Python 3.12 with only the job's apt and pip installs) | migration check: no changes; **1,142 backend tests, OK**, 1 skipped (note 3); AI suite **88, OK** |
| Backend suite in the release image (ML, LLM and local-model layers, built from `constraints.txt`), source mounted read-only | **1,142 tests, OK**, 1 skipped (note 3); nothing written into the source tree |
| `frontend` (Node 22) | typecheck clean; **61/61** while both backend suites loaded the machine (on 3ebf20d, 57/57 three runs in a row under load); production build; `npm audit --omit=dev`: 0 vulnerabilities |
| `security` | `check --deploy --fail-level WARNING`: no issues; bandit 1.9.4: clean; pip-audit 2.10.1: none; `.dockerignore` gate: passes on the committed file (note 1) |
| The pinned set itself (`pip-audit -r constraints.txt --no-deps`) | no known vulnerabilities; torch's CPU build cannot be audited against PyPI. CI's own audit resolves `requirements.txt` to the newest releases, not to the pins |
| `ai-regression`, as the job runs it: fresh database, corpus ingested through the pipeline | v1: 13 cases, **no change** against `baseline.json`. v2: 57 cases, recall@1 0.796, **no change** against `baseline-v2.json`. Synthetic data: this shows nothing regressed, not that answers are right |
| `e2e`, with the job's own steps: `.env` from `.env.example`, `docker compose up -d --build`, `seed_e2e`, the Vite dev server, Playwright's Chromium | **7 passed, 1 skipped by design** (the CSP probe: Vite sends no policy), no dev-server reload. Only the ports differed from CI (18000 and 15173; other servers held 8000 and 5173). Before fix 17, the same steps failed twice |
| `build` | the backend image as production builds it (f451ccb; nothing after it changes a dependency or the Dockerfile, and the suites above ran 9f2772b's code in it), and one with every optional layer, S3 and OpenTelemetry included (3e02d9d): both build, `pip check` clean, every package at its pinned version, uid 10001. Web edge image (9f2772b): builds, and the frontend checks inside it pass, 61/61 (note 2). `check_deployment_config.py`: pass |
| Workflow file | actionlint 1.7.7 with shellcheck: no errors; 2 style warnings (an unused loop counter) |
| Prometheus files | promtool: configuration valid, 13 alert rules |
| Image contents: the release images above and every other local image of this project, development images included | no document, dump, database, key or `.env` file outside installed packages; `/app/backend/media` empty; application images run as uid 10001 |

1. On a Windows checkout (`core.autocrlf=true`) `.dockerignore` is written with CRLF, and
   the gate's exact-line `grep -x` then fails on every entry. Docker itself trims the CR,
   and CI checks the file out as committed (LF), where the gate passes.
2. The edge image runs the frontend tests while it builds. Before fix 18, while the laptop
   ran three image builds and a test suite at the same time, 5 of 57 failed waiting for a
   page to render. With fix 18 the image built cleanly, and the suite passed under load.
3. The skipped test is `SamplerTests`, which needs the optional OpenTelemetry layer;
   neither CI nor the production image installs it by default. In the image built with
   every optional layer, all 11 tracing tests pass, that one included.

#### Migrations

Both branches added a `documents` migration numbered 0018 against 0017:
`0018_auditlog_document_department_db_default` (a database default for one column) and
`0018_collection` (a new table, and wider choices for `AuditLog.action`). They touch
different columns. `0019_merge_auditlog_db_default_and_collection` joins them. A merge
node, not a renumbering, so a database that already applied either 0018 keeps a valid
history. On a disposable database:

- `makemigrations --check`: no changes; `migrate --check`: nothing unapplied;
- a fresh database, one first migrated to `0018_collection` (as `platform-completion`
  left it), and one first migrated to the other 0018 (as `release-review-fixes` left it)
  all end with the same `pg_dump --schema-only`;
- `documents` migrates back to 0017 and forward again to the same schema.

**Earlier runs.**

| What | Result | When |
|---|---|---|
| CI's `e2e` configuration (hashing embeddings, lexical reranking, the Vite dev server) | Playwright **7 passed**, 1 skipped by design (the CSP probe; Vite sends no policy), on bb34379 | 2026-10-10 |
| Production stack built fresh with the pilot AI configuration, browser suite through the TLS edge | **8/8** on e34d1c7; Prometheus 4/4 targets up | 2026-10-10 |
| Backend suite in the image built from `constraints.txt`, source mounted read-only | **971 tests, OK**; **974** with the review fixes below | 2026-10-06; 2026-10-10 |
| New regression tests against the code before the fixes | all 8 quarantine and audit tests **fail**, proving they test the defect | 2026-10-06 |
| Lint and format (ruff 0.16.8) | clean | 2026-10-06 |
| Release-candidate production stack (fresh volumes, nginx with a self-signed certificate, pilot AI configuration, `HF_HUB_OFFLINE=1`) | health, readiness, HTTP→HTTPS 301, anonymous API 401, edge `/metrics` 404, CSP, `DENY` and `nosniff` headers | 2026-10-06 |
| Browser suite + CSP probe through nginx over TLS | **8/8**, twice | 2026-10-06 |
| AI regression gates v1 and v2, exactly as CI runs them | **both pass**; v2 at its baseline | 2026-10-06 |
| Backup → restore into a new database → compare | verified; 34 tables; documents, versions, chunks, embeddings, audit rows and users identical | 2026-10-06 |
| Rollback across a migration, using the runbook's commands verbatim | previous release → candidate → previous release from the pre-upgrade dump; healthy; sign-in works | 2026-10-06 |
| Restart of backend and worker | healthy, no permission errors | 2026-10-06 |
| Worker queues; Prometheus targets | both workers on both queues; **4 of 4 targets up** | 2026-10-06 |
| Deployment configuration check; CI YAML parses | pass; 10 jobs | 2026-10-06 |
| Image contents | uid 10001; no `.env`, dumps, virtualenvs, media or compiler; code not writable | 2026-10-06 |
| Dependency audit (`pip-audit` of the image) | **no known vulnerabilities**; torch's CPU build cannot be audited against PyPI | 2026-10-06 |
| Earlier: 3,000-document ingestion and latency measurement; host-reboot survival; `--scale backend=2` | see [performance](../operations/performance.md) and the [runbook](../operations/runbook.md) | 2026-10-04/05 |

### Repository integrity

| Check | Result |
|---|---|
| Branches (2026-10-12) | **`release/pilot`**: `platform-completion` at 6641009, plus the merge of `release-review-fixes` and fixes 15–18. `platform-completion` and `release-review-fixes` are kept unchanged. `master` is an ancestor of all three, 43 commits behind `platform-completion`. Nothing is pushed: **there is no remote** |
| Other refs | two `refs/codex/…` refs, left by another tool, point at snapshots of the working tree (388 files; nothing sensitive in them). Push branches by name: `git push --mirror` would publish these too |
| Working tree | `release/pilot` is clean. The development checkout has uncommitted edits that are not part of this release (raising `MAX_DOCUMENT_PAGES` to 2000, a README rewrite, a stylesheet change, and the Gemini block in `.env.example` uncommented) |
| Tracked files | 391. The only binary is `frontend/e2e/fixtures/scanned-circular.png`, a synthetic OCR fixture. No document, dump, database, certificate, key or `.env` file in the tree, or ever added on any branch (re-checked 2026-10-12) |
| Secrets | none in tracked files or in any commit on any branch (pattern scan, 2026-10-12: AWS, Anthropic, OpenAI-style, Google API, GitHub and Slack tokens; private keys). `.env` is gitignored and excluded from images. CI uses only labelled placeholders |
| Generated data | seeded benchmarks, the corpus manifest, media and backups are gitignored and excluded from images (the manifest since fix 15) |
| **Local Docker images** | The images built about 2026-09-22 as `ai-document-intelligence-backend:latest` and `-worker:latest`, which contained a real institutional PDF and ran as root, **no longer exist** (checked 2026-10-12): no image of this project older than 2026-10-05, no dangling image, and no build cache older than 2026-10-10. Those tags now name development images rebuilt since, which run as uid 10001. Every local image of this project was scanned on 2026-10-12, and none contains a document. Development images are rebuilt often, so scan any image again immediately before pushing it to a registry (checklist step 19). The `adi-rc-*` images were deleted; `adi-perf-*` is the same 2026-10-06 build |

## EXTERNALLY UNVERIFIED

**(C)** None of these has happened. Each needs something outside this repository.

| Item | Needs | How it gets verified |
|---|---|---|
| CI on GitHub | a GitHub repository | a first green run of all 10 jobs |
| Deployment on a real server | a server | [staging runbook](staging-runbook.md), steps 1–19 |
| Let's Encrypt issuance and renewal | a public hostname, ports 80 and 443 open | the [TLS test plan](staging-runbook.md#tls-test-plan), T1–T11 |
| Off-host backup | a destination and an encryption key | staging step 16, then restore from the off-host copy |
| Production disaster recovery | production | a timed restore of a production-sized dump on a fresh host |
| S3 or MinIO storage | a bucket | [multi-host-validation.md](multi-host-validation.md), section A |
| A second application host | shared Postgres and Redis, a load balancer | the same page, section B |
| Hosted answerers (Claude, Bedrock, OpenAI-compatible) | credentials and an approval | [ai-release.md](ai-release.md#hosted-answerer-activation) |
| Alert delivery and external uptime monitoring | an alert channel and a monitor | [post-go-live monitoring](#post-go-live-monitoring) |
| Behaviour under concurrent users | a load test on the staging hardware | not yet planned |

## REQUIRED CREDENTIALS

Nothing here exists yet, and none of it goes into the repository or into CI.

| Credential | Where it lives | Needed for |
|---|---|---|
| `DJANGO_SECRET_KEY`, different per environment | `.env` on the host | always |
| `POSTGRES_PASSWORD` | `.env` | always. It is read only when the database volume is first created |
| `GRAFANA_PASSWORD` | `.env` | always; compose refuses to start without it |
| `CERTBOT_EMAIL` (not secret) | the shell running `init-letsencrypt.sh` | certificate issuance and expiry mail |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | `.env` | recommended. Nothing sends mail yet, but Django's deploy check flags the console backend |
| Off-host backup destination and encryption key | the backup system | backups that survive the host |
| SSH access for operators | the server | operation; the tunnel to Grafana |
| **Only if approved:** `ANTHROPIC_API_KEY`; or AWS credentials with Bedrock access; or an OpenAI-compatible provider's key | `.env` | a hosted answerer |
| **Only if S3 is used:** AWS credentials limited to one bucket | `.env` or an instance role | object storage |

**The CI workflow references no secrets.** Every value it uses is a labelled placeholder
for disposable databases. Its deploy jobs are placeholders that print a message. It pushes
no images (`push: false`), so no registry credentials are needed.

## REQUIRED INFRASTRUCTURE

| Item | Specification |
|---|---|
| Staging server, then production server | Linux x86_64. At least 4 vCPU (8 recommended), 8 GiB RAM, 40 GB disk plus uploads × backup retention. Docker Engine with Compose **2.24.4 or later** ([step 1](staging-runbook.md#1-server)) |
| Hostnames and DNS | an A record per environment. AAAA only after checking IPv6 reaches nginx |
| Firewall | inbound 80 and 443 (80 is required for HTTP-01 and renewal); 22 from administrators only |
| Off-host backup storage | encrypted, outside the server's failure domain |
| External uptime and certificate-expiry monitor | watching `/health/` and the certificate |
| Alert channel | for the alerts in [post-go-live monitoring](#post-go-live-monitoring) |
| SMTP relay | recommended |
| GitHub repository | see below |

**GitHub configuration, derived from `.github/workflows/ci.yml`:**

| Setting | Value | Why |
|---|---|---|
| Repository | an owner, a visibility and a plan: decision [I6](institutional-decisions.md) | private repositories get required reviewers only on GitHub Enterprise (I5) |
| Default branch | `main` or `master` | `push` triggers only on these two; `build`, `ai-regression` and the deploy chain run only on a push to them |
| What to push | `release/pilot`, as the default branch | a push to any other branch name starts no workflow run at all |
| Actions | enabled; GitHub-hosted `ubuntu-latest` runners | service containers `pgvector/pgvector:pg16` and `redis:7-alpine`; actions `checkout@v4`, `setup-python@v5`, `setup-node@v4`, `upload-artifact@v4`, `docker/setup-buildx-action@v3`, `docker/build-push-action@v6`; GitHub Actions cache |
| Environment `staging` | create it **before the first push** | used by `deploy-staging`. A run that names a missing environment creates it, with no protection rules |
| Environment `production` | create it **with required reviewers**, before the first push | used by `deploy-production`; this is the approval gate. Created by the first run instead, it has no reviewers, and the job runs unapproved (today it only prints a message) |
| Repository variable `STAGING_URL` | `https://<staging-host>` | used by `smoke`. **When it is unset, the smoke job passes without testing anything** |
| Secrets | none | the workflow uses none |
| Branch protection (recommended) | require `lint`, `tests`, `frontend`, `e2e` and `security` on pull requests | the workflow cannot enforce this itself |

The deploy jobs do not deploy. Deployment is the manual [staging runbook](staging-runbook.md)
until someone wires the deploy jobs to the host, for example with SSH and the runbook's
upgrade commands. Until then, a green pipeline means "safe to deploy", not "deployed".

## INSTITUTIONAL DECISIONS

**(D)** Full list, with what the code does meanwhile:
[institutional-decisions.md](institutional-decisions.md). These block staging or the
pilot:

| # | Decision | When |
|---|---|---|
| I6 | where the repository lives: owner, visibility, plan | before the first CI run |
| S4 / O4 | certificate source; server and hostnames | before staging |
| I1 | sign-in method: local accounts for the pilot, VPN or proxy, or SSO first | before pilot |
| I2 | whether `/django-admin/` is reachable from the internet | before pilot |
| I3 | the first administrators and the departments | before pilot |
| D1, D5 | the pilot corpus and its languages | before pilot |
| D6, D7 | who writes the institutional evaluation questions; the acceptance thresholds | before pilot |
| S1 | which malware scanner | before pilot |
| O1 | off-host backup destination, encryption and key custody | before pilot |
| O3 | where alerts go and who responds | before pilot |

## REAL DATA REQUIREMENTS

**(E)** No institutional data exists in this repository, and none was invented. Before any
quality claim, the institution needs to do the following
([plan](ai-release.md#real-institutional-data-plan)):

1. Choose a pilot corpus from one or two departments, with sensitivity and language
   recorded and personal data excluded unless approved.
2. Have staff who use those documents write 50–100 evaluation questions with their answer
   locations, each checked by a second person. Keep the set inside the institution.
3. Run both rerankers, and any approved hosted answerer, on that set. Store the results
   as the institution's baseline.
4. Review live answers each week during the pilot (30 answered, 20 declined), and decide
   against the [acceptance criteria](ai-release.md#acceptance-criteria-proposed-the-institution-sets-the-numbers).

Real data also settles what nothing here can:

- OCR quality on the institution's scans;
- processing failure rates by document type;
- non-English content;
- whether classification needs a model trained on institutional document types.

## SECURITY GAPS

The security audit reviewed 30 areas against the code.

**Fixed and verified by tests:**

- authentication: one generic failure message, inactive accounts refused, session
  rotation, per-address and per-account throttles;
- session cookies: HttpOnly, Secure, SameSite=Lax, 8-hour absolute lifetime;
- CSRF, including on login;
- API token expiry and revocation;
- password change and administrator reset;
- no privilege escalation;
- department isolation across documents, search, chat, audit, evaluation, dashboard and
  jobs;
- authorized, audited downloads, with media never served statically;
- upload limits at the application and at nginx;
- rate limits;
- audit coverage;
- CSP and security headers; CORS;
- nginx exposure (`/metrics`, `/media`);
- the database not published;
- non-root containers;
- secrets handling;
- insecure-if-unset settings;
- log redaction;
- prompt-injection defences;
- and, in this audit, the quarantine bypass and the audit-subject loss (blocking), plus
  the placeholder secret key.

**Known gaps** (accepted for a single-host pilot by this review; the institution may
decide otherwise):

| Gap | Impact | Mitigation |
|---|---|---|
| The heuristic scanner is not an antivirus | it catches disguised executables, scripts and EICAR, and only flags macros and PDF active content | integration point for ClamAV or a commercial scanner (decision S1) |
| A new upload is downloadable before its scan completes | a flagged file is reachable by its readers for the minutes before a worker scans it | decision S2 |
| Redis has no password | any container on the compose network could read sessions; nothing outside can reach it | single host only; a password is required before any second host |
| API token keys are stored in plaintext (DRF); one token per user | a database read yields live tokens, valid for at most 12 h | protect database access and backups (the backups contain them too) |
| No encryption at rest in the application | the disk holds documents and dumps in the clear | encrypted disks (decision S6); encrypted off-host backups |
| Audit rows and chat history are kept forever; a deleted document's title stays on its delete row | retention | decisions D3 and D4 |
| CI dependency audits are advisory | a new CVE is reported, not blocking | re-run `pip-audit` against the image before each release |
| `certbot/certbot:latest` and `nginx:1.27-alpine` float | an upstream change could alter behaviour | record digests at staging; pin production to them |
| HSTS is off | a first visit over HTTP could be intercepted | decision S3, after the certificate has renewed once |

**Require an institutional decision:**

- SSO, MFA and lockout (I1);
- exposure of `/django-admin/` (I2);
- HSTS (S3);
- Docker hardening beyond non-root: capabilities, read-only root filesystem, network
  segmentation (S5);
- sending document text to a hosted AI provider (D2);
- the malware engine (S1);
- retention (D3, D4).

## AI QUALITY LIMITATIONS

Details: [ai-release.md](ai-release.md).

- **System engineering quality is verified; AI model quality on institutional documents
  is unknown.** The only measurements are on 57 synthetic questions over 9 synthetic
  documents.
- With the recommended configuration, the system answers about half of answerable
  questions (coverage 0.491 on v2), and declines most of the rest. The extractive answerer
  declines paraphrased questions even when it retrieved the right passage. **Expect many
  refusals; a refusal does not mean the answer is absent.**
- When it answers, it quotes verbatim with a citation. On v2: R@1 0.980, citation
  accuracy 0.875, 2 of 8 unanswerable questions answered anyway.
- English only, as far as anyone knows. Scanned-document quality at scale is unmeasured.
  Classification predicts business-document classes, not institutional ones.
- **Reranker decision for the pilot: MiniLM-L6 at its calibrated floor of 0.30.** On v2 it
  ranks better than bge-reranker-base (R@1 0.980 vs 0.857) and is about ten times faster
  (benchmark p50 644 ms vs 6.2 s). Through the edge on 3,000 documents, chat took 1.57 s
  at the median with MiniLM against 13.8 s with bge-reranker-base on the test
  laptop. The cost is 3 fewer answered questions out of 57, on synthetic data. Revisit on
  the institutional evaluation set, when a GPU is available, or if documents are not in
  English.
- **Hosted answerer: off.** No deployment has called one. In development the Gemini
  preset was run against Google's free tier (2026-10-11, a developer's own key); no
  benchmark has been run through a hosted answerer. Turning one on requires all of:
  - a provider decision with data-processing terms (D2);
  - if chat should also reply from the model's own knowledge, decision D8
    (`CHAT_GENERAL_KNOWLEDGE`, off by default);
  - credentials;
  - a provider-side spend cap;
  - a timeout budget below nginx's 120 s. The defaults allow 180 s per call, which would
    turn a slow provider into 504s;
  - an evaluation run with zero fallbacks.

## PERFORMANCE LIMITATIONS

From [performance.md](../operations/performance.md) and [evaluation](../evaluation/README.md).
All figures are from one laptop with one sequential client.

| | Figure |
|---|---|
| **MEASURED** | Ingest acceptance 15.0 documents/s. Processing 1.73 documents/s on 2 worker processes (text documents; per-job p50 659 ms, p95 2.5 s). Search p50 158–160 ms, p95 about 380 ms at 3,003 documents. Document list p50 49–72 ms; dashboard 42–68 ms. **Chat p50 13.8 s with bge-reranker-base** (13.4 s of it reranking). In-process benchmark p50 644 ms with MiniLM vs 6.2 s with bge-reranker-base. Exact vector query 61 ms over 6,547 chunks. Backend 2.1 GiB, ingest worker about 1 GiB at about 5.3 cores. **With the recommended MiniLM reranker: chat p50 1.57 s, p95 3.26 s** (rerank 1.32 s), search p50 153 ms; backend 1.2 GiB idle; the idle index worker 2.3 GiB at its default 6 processes (set `CELERY_INDEX_CONCURRENCY=1`). Restore of a 153 KB dump: 10.4 s including container start |
| **ESTIMATED** | Vector search about 0.25 s at 12,000 documents and about 1 s at 100,000 chunks (linear extrapolation from one point). About 0.15 s per document of model reload cost. Disk sizing in [step 1](staging-runbook.md#1-server) |
| **UNKNOWN** | Any concurrent load (one attempt was contaminated by other work on the laptop and is not reported). Scanned (OCR-heavy) corpora at scale. Anything beyond 3,003 documents. Server-class or GPU hardware. Hosted-provider latency. Restore time for a production-sized database |

No optimisation was made for bottlenecks that have not been observed. The first
measurement on staging should be chat latency with MiniLM, and a small concurrent load.

## STAGING PROCEDURE

Follow [staging-runbook.md](staging-runbook.md) in order:

1. server, Docker and DNS;
2. firewall;
3. `.env` and secrets;
4. database and Redis;
5. local storage;
6. build;
7. TLS: Let's Encrypt's staging CA first, then real issuance;
8. migrations and the first administrator;
9. workers, `warm_models`, then `HF_HUB_OFFLINE=1`;
10. health checks;
11. monitoring;
12. backups with an off-host copy;
13. restore verification;
14. rollback rehearsal;
15. smoke tests.

Record every result in the staging log. Steps 13, 15–17 and 18 were rehearsed locally on
the release candidate; everything needing a real server, DNS or a public certificate has
never run.

## ROLLBACK PROCEDURE

[Staging runbook, step 18](staging-runbook.md#18-rollback).

**Before every upgrade:**

- tag the running images `:rollback`;
- take a `pre-upgrade` backup;
- record the commit.

**To roll back:**

1. Stop the edge, backend and workers.
2. Retag the `:rollback` images as `:latest`.
3. Check out the previous commit.
4. If the new release ran migrations, restore the `pre-upgrade` dump. Anything written
   since the upgrade is lost.
5. `up -d --no-build`.

Rehearsed locally on 2026-10-06 across a real migration (0016 → 0017 → 0016), using the
commands verbatim. If the tagged images are lost, the previous tag rebuilds with the same
Python packages, because of `constraints.txt`. The base image and Debian packages may
differ.

## BACKUP/RESTORE PROCEDURE

- **Back up:** `manage.py backup --include-media --prune` daily from host cron. It runs
  `pg_dump` (custom format), reads the dump back, and records its SHA-256 and the
  migration state. It archives the media volume and keeps 7 copies.
- **Copy off the host,** encrypted ([step 16](staging-runbook.md#16-backups)). Nothing in
  the repository does this.
- **Restore:** `manage.py restore <dump>` reports without changing anything; `--yes`
  replaces the tables. It refuses a dump whose checksum or manifest is wrong, and reports
  schema skew. To verify a backup, restore it into a **separate disposable database**
  ([step 17](staging-runbook.md#17-restore-verification)).
- With S3, `--include-media` archives nothing. The originals then depend on bucket
  versioning ([multi-host-validation.md](multi-host-validation.md)).

| Level | Status |
|---|---|
| **LOCALLY RESTORE-VERIFIED** | **Yes.** 2026-09-30: restored into an empty database, row counts matched, hybrid search and sign-in worked on the copy. 2026-10-06: the runbook's backup and restore-verification commands rehearsed verbatim on the release candidate, with identical counts, plus a rollback restore |
| **OFF-HOST BACKUP VERIFIED** | **No.** No off-host destination exists |
| **PRODUCTION DR VERIFIED** | **No.** No production exists; RTO is unmeasured beyond a 157 KB dump |

## GO-LIVE CHECKLIST

1. Decide where the repository lives (I6). Create it, then create the `staging` and
   `production` environments, with reviewers on `production`, **before** pushing. Set
   `STAGING_URL` once staging exists. Push `release/pilot` as the default branch
   (`git remote add origin <url>`, then `git push origin release/pilot:main`), branches by
   name and never `--mirror`. **Get one green run of all jobs.**
2. Record the before-staging and before-pilot [institutional decisions](institutional-decisions.md).
3. Provision the staging server. Set up DNS (A record) and the firewall (22 restricted;
   80 and 443 open).
4. Write `.env` with unique secrets (`chmod 600`). Set `CELERY_INDEX_CONCURRENCY=1`. Leave `RAG_ANSWERER` unset and
   `INSTALL_S3=false`.
5. Run `check_deployment_config.py`, then build.
6. TLS: run test plan T1–T11. Staging CA first, then real issuance, then a renewal
   dry-run.
7. Migrate and create the first administrator. Run `warm_models`, then set
   `HF_HUB_OFFLINE=1`.
8. Install the cron jobs: daily backup, encrypted off-host copy, and
   `recover_stalled_jobs` every 15 minutes.
9. Verify a restore into a disposable database, and time it.
10. Rehearse a rollback on staging.
11. Run the smoke tests (step 19). They must include the department boundary, the audit
    trail, and a restart.
12. Wire monitoring: deliver the shipped alert rules to the agreed channel (O3), check the
    dashboard against the table below, and add external uptime and certificate-expiry
    checks.
13. Record the image digests for `certbot/certbot` and `nginx:1.27-alpine`, and pin
    production to them.
14. Build the institutional evaluation set. Run it, and decide against the acceptance
    criteria. Alternatively, record a decision to pilot with the known limits.
15. Production: repeat steps 3–12 with the production hostname and new secrets. Approve
    through the `production` environment.
16. Create real administrators and departments. **Never run `seed_e2e` on production.**
17. Tell pilot users what to expect: quoted answers with citations, frequent refusals,
    English documents.
18. After the first successful certificate renewal, decide on HSTS (S3).
19. Before any registry push, scan the exact image being pushed for documents and secrets
    (its `/app/backend/media` must be empty), and push only images built from a clean
    checkout. The old images that held a real document no longer exist (2026-10-12), but
    development images are rebuilt from working trees that hold uploads.

## POST-GO-LIVE MONITORING

**Since 2026-10-11, 13 alert rules (`infrastructure/prometheus/alerts.yml`) and an overview
dashboard ship; nothing delivers an alert.** There is no Alertmanager and no Grafana
contact point. Check the shipped rules and panels against this table, and wire delivery
to the agreed channel (O3) before go-live. Grafana is reached through an SSH tunnel to
127.0.0.1:3000.

| Signal | Query | Alert when |
|---|---|---|
| Backend down | `up{job="adi-backend"}` | `== 0` for 2 min |
| Workers down | `up{job="adi-workers"}` | `== 0` for 5 min |
| Server errors | `sum(rate(adi_http_requests_total{status=~"5.."}[5m])) / sum(rate(adi_http_requests_total[5m]))` | `> 0.01` for 10 min |
| API latency | `histogram_quantile(0.95, sum by (le) (rate(adi_http_request_duration_seconds_bucket[5m])))` | above the agreed target |
| Chat latency | `histogram_quantile(0.95, sum by (le) (rate(adi_rag_duration_seconds_bucket[15m])))` | above the agreed target (for example 10 s) |
| Ingest backlog | `adi_queue_depth{queue="ingest"}` | growing for 15 min |
| Processing failures | `sum by (error_code) (increase(adi_pipeline_failures_total[1h]))` | any `TIMEOUT` or `INFRASTRUCTURE_ERROR` |
| Quarantines | `increase(adi_malware_verdicts_total{verdict="INFECTED"}[1d])` | `> 0`: review who uploaded what |
| Reranker degraded | `increase(adi_rerank_degraded_total[15m])` | `> 0` |
| Hosted answerer failing (if enabled) | `increase(adi_llm_fallbacks_total[15m])` | `> 0` |
| Abstention rate | `sum(rate(adi_rag_abstentions_total[1d])) / sum(rate(adi_rag_requests_total[1d]))` | trend only: a sudden change means retrieval changed |
| Sign-in attacks | `increase(adi_login_failures_total[15m])`, `increase(adi_throttled_requests_total[15m])` | a spike (`adi_auth_failures_total` also counts every 401, including the web app's signed-out session check) |
| Cross-department probing | `increase(adi_authz_denials_total[1h])` | a spike; investigate in the audit log |
| User feedback | `sum by (rating) (increase(adi_answer_feedback_total[1d]))` | trend only |

**Outside Prometheus.** These are not covered by anything shipped:

- disk free on the host;
- certificate expiry (the external monitor);
- **backup freshness**: the newest `backups/db-*.dump` is less than 26 hours old, and an
  off-host copy exists;
- the backup and stalled-job cron logs.

**Human checks:**

- daily: the System status screen, Processing failures, the backup log;
- weekly: denied attempts in the audit log, a restore of the latest backup into a
  disposable database, and during the pilot the human review sample;
- monthly: `pip-audit` of the image, and a rebuild to pick up base-image fixes.

## OPTIONAL FUTURE IMPROVEMENTS

**(F)** None of these blocks the pilot. Each should wait for evidence that it is needed.

- Alert delivery (Alertmanager or Grafana contact points), once O3 names a channel.
- Hold downloads and previews until the scan completes (if S2 is decided that way).
- SSO and MFA (if I1 requires them).
- Hashed API tokens.
- Retention jobs for audit rows and chat history.
- A ClamAV integration behind `DOCUMENT_SCANNER`.
- A host-B compose profile and a Redis password, if a second host is ever needed.
- An S3 `addressing_style` setting, if MinIO needs it.
- An HNSW query path, measured against today's exact scan, only once corpora approach
  about 100,000 chunks.
- LLM telemetry: per-model token labels, latency buckets above 10 s, and the fallback
  count printed by `evaluate`.
- An `ANTHROPIC_BASE_URL` guard to match the OpenAI-compatible one.
- An Ollama service in compose, if a local generative model is chosen.
- A classifier trained on institutional document types (needs labelled real data).
- Multilingual models (if D5 requires them).
