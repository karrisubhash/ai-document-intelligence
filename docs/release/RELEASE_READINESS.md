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
| Local verification | **COMPLETE** | 974 backend tests; 8/8 browser tests through nginx over TLS on the release candidate, with the pilot configuration; both AI gates; backup, restore and rollback rehearsed |
| CI | **NOT YET VERIFIED** | There is no GitHub repository, so the workflow has never run. Its commands were run locally |
| Staging | **BLOCKED** | It needs a server, a hostname and DNS, and the institution's before-staging decisions. No code change is required |
| Production | **BLOCKED** | It needs staging to pass, plus the before-pilot decisions |
| AI | **NEEDS REAL DATA** | The engineering is ready for a pilot: grounded, permission-filtered, and it declines rather than guesses. Quality on the institution's documents is unmeasured |
| Security | **No known release blocker** | 2 blocking defects found by the audit and fixed with tests. Known gaps are documented; 7 security decisions belong to the institution |
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
- the evaluation framework, with two regression gates.

**Fixed by this audit.** Each code fix has a test that fails without it.

| # | Defect | Consequence | Fix |
|---|---|---|---|
| 1 | Production dependencies were unpinned (`>=`) | a rebuild installed whatever was newest, so staging and production could run untested code | `backend/constraints.txt` pins all 90 packages of the tested image. Every install, in the image and in CI, uses it. Verified: the rebuilt image's packages are identical to the file |
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

The same review raised two more findings, which another session working in this repository took on in the shared checkout; they were not yet committed at the time of writing:

- the audit list's `?document=` and `?user=` filters return 500 for digits such as `²`;
- when a version that once completed is later quarantined, its earlier extracted text stays searchable. Download is the only path that checks quarantine. Pre-existing.

Fixes 12–14 are on branch `release-review-fixes`, to be merged into `platform-completion` once that session's changes are committed.

## LOCALLY VERIFIED

**(B)** All of this ran on the development laptop: an i5-8350U, Docker Desktop, an 8-CPU
and 7.7 GiB VM.

| What | Result | When |
|---|---|---|
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
| Branch | `platform-completion`, ahead of `master`. Nothing is pushed: **there is no remote** |
| Working tree | clean after the release commits |
| Tracked files | 364. The only binary is `frontend/e2e/fixtures/scanned-circular.png`, a synthetic OCR fixture. No documents, dumps, databases, certificates or keys in the tree or in history |
| Secrets | none in tracked files (pattern scan: API keys, AWS keys, private keys, tokens). `.env` is gitignored and excluded from images. CI uses only labelled placeholders |
| Generated data | seeded benchmarks, corpus manifests, media and backups are gitignored and excluded from images |
| **Local Docker images** | `ai-document-intelligence-backend:latest` and `-worker:latest`, two weeks old, contain a real institutional PDF in `/app/backend/media/` and run as root. **They must never be pushed to any registry.** Recommended: delete them (`docker rmi`); that is the owner's call. The `adi-rc-*` images are the verified release candidate |

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
| Default branch | `main` or `master` | `push` triggers only on these two; `build`, `ai-regression` and the deploy chain run only on a push to them |
| Actions | enabled; GitHub-hosted `ubuntu-latest` runners | service containers `pgvector/pgvector:pg16` and `redis:7-alpine`; actions `checkout@v4`, `setup-python@v5`, `setup-node@v4`, `upload-artifact@v4`, `docker/setup-buildx-action@v3`, `docker/build-push-action@v6`; GitHub Actions cache |
| Environment `staging` | create it | used by `deploy-staging` |
| Environment `production` | create it **with required reviewers** | used by `deploy-production`; this is the approval gate |
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
- **Hosted answerer: off.** It has never been called. Turning it on requires all of:
  - a provider decision with data-processing terms (D2);
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

1. Create the GitHub repository and push. Create the `staging` and `production`
   environments, with reviewers on `production`, and set `STAGING_URL`. **Get one green
   run of all jobs.**
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
12. Wire monitoring: the panels and alerts below, delivered to the agreed channel, plus
    external uptime and certificate-expiry checks.
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
19. Before any registry push, delete the old local images that contain a real document
    (`ai-document-intelligence-backend:latest`, `-worker:latest`).

## POST-GO-LIVE MONITORING

**No alert rules or dashboards ship.** Build these in Grafana (reached through an SSH
tunnel to 127.0.0.1:3000). Wire the alerts to the agreed channel before go-live.

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
| Sign-in attacks | `increase(adi_auth_failures_total[15m])`, `increase(adi_throttled_requests_total[15m])` | a spike |
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

- Alert rules and Grafana dashboards as code, from the table above.
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
