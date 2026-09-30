# Operations runbook

Commands assume the production stack; prefix with
`docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend`
(or `exec backend`) to run them inside the application image.

## Is it up?

| Probe | Meaning | Use it for |
|---|---|---|
| `GET /health/` | process up, database reachable | liveness — restart on failure |
| `GET /ready/` | database, cache and broker all answer | readiness — take out of rotation on 503; never restart on it |
| **System status** screen (`/admin/system` in the app) | all of the above plus storage write/read, queue depths, stalled runs, failures in the last 24 h, the AI configuration | a human's first look |
| `/metrics` | Prometheus, private networks only | dashboards and alerts |

Signals worth alerting on (all bounded-label metrics):

- `adi_rag_abstentions_total{reason}` rising for `retrieved passages were not relevant
  enough` or `could not be grounded` — retrieval or answering quality has moved.
- `adi_llm_fallbacks_total` > 0 — a configured generative answerer is failing and answers
  are silently extractive.
- `adi_rerank_degraded_total` > 0 — the cross-encoder is failing; ranking is lexical.
- `adi_queue_depth{queue="ingest"}` growing for more than a few minutes — add `worker`s.
- `adi_pipeline_failures_total{error_code="TIMEOUT"}` — documents exceeding
  `CELERY_TASK_SOFT_TIME_LIMIT` (15 min).

## Processing

Failed and stalled runs are listed under **Administration → Processing**, with the stage,
error code and a Retry button (current versions only; quarantined files are never retried).

A run is **stalled** when it is `PROCESSING` and its row has not changed for
`STALLED_JOB_MINUTES` (120): its worker was killed — OOM, a hard time limit, a node loss —
and nothing marked it finished. From cron, every 15 minutes:

```bash
python manage.py recover_stalled_jobs --apply --requeue
```

Without `--apply` it only reports. `QUEUED` jobs are never treated as stalled: a long
backlog is not a failure. A stalled job's message, if it is ever redelivered, is skipped.

| Error code | Meaning | Action |
|---|---|---|
| `UNSUPPORTED_FILE_TYPE`, `CORRUPT_DOCUMENT`, `EMPTY_EXTRACTION`, `TOO_MANY_PAGES` | the file itself | ask the owner for a better copy; not retryable |
| `MALWARE_DETECTED` | quarantined by the scanner | review the scan findings in the timeline; never retry |
| `TIMEOUT` | exceeded the soft time limit | split the document, or raise the limit after measuring |
| `STALLED` | worker lost | Retry |
| `INFRASTRUCTURE_ERROR` | database or storage blip | retried automatically with backoff; Retry if it persists |

## Re-indexing

After changing `DOCUMENT_EMBEDDING_PROVIDER`, `CHUNK_SIZE` or `CHUNK_OVERLAP`:

```bash
python manage.py reindex --dry-run          # what would be queued
python manage.py reindex --stale-only       # only content embedded by another model
```

It runs on the `index` queue, so uploads are not delayed behind it. Search keeps working
throughout: it only compares vectors from the configured model, and content not yet
re-embedded is still found lexically.

## Models

```bash
python manage.py warm_models      # download (first time) and load the configured models
```

Run after a deploy that changes a model, then set `HF_HUB_OFFLINE=1`.

## Backups and restore

```bash
python manage.py backup --prune                    # pg_dump, verified, manifest + sha256
python manage.py backup --include-media --label pre-upgrade
python manage.py restore /backups/db-<stamp>.dump          # reports; changes nothing
python manage.py restore /backups/db-<stamp>.dump --yes    # replaces the tables
```

Every dump is read back with `pg_restore --list` before success is reported, and its
SHA-256 and per-app migration state are recorded in a manifest. Restore refuses a dump
whose checksum does not match its manifest, verifies afterwards that every table is
present, and reports any app whose code is ahead of the restored schema (run `migrate`).

Media (uploaded files) lives in the `media_data` volume or the bucket. `--include-media`
archives the volume; with S3, use the bucket's own versioning and replication instead.

**Schedule**: cron `python manage.py backup --prune` daily (retention `BACKUP_RETENTION`,
default 7), and copy `./backups` off the host — a backup on the same disk as the database
protects against mistakes, not against losing the disk.

### Restore drill (performed)

Performed on disposable databases on the development host, not on production data:

| Step | Result |
|---|---|
| `backup` of a database holding the end-to-end corpus (10 documents, 15 chunks with 384-d vectors, 167 audit rows, 6 users) | 157 KB, 34 tables, verified; 4.0 s including container start |
| `restore --yes` into a new, empty database | checksum OK, 34 tables present; 5.5 s |
| Row counts of documents, versions, pages, chunks, embeddings, audit, messages, departments, users | identical to the source |
| Hybrid search as a restored student account | 10 hits, correct top document, lexical **and** vector candidates — the pgvector data and full-text index are usable |
| Password hashes | a restored account still authenticates |

RPO is the backup interval (24 h with a daily cron) plus whatever is not yet copied off
the host. RTO for this data size is seconds; **it has not been measured at institutional
scale**, and it grows with the dump size — measure it on a production-sized copy before
committing to a number.

## Production stack smoke test

**Last run (local, 2026-09-30):** the full production compose stack — the built web and
backend images, gunicorn, Celery `ingest` and `index` workers, nginx with a self-signed
certificate for `localhost`, Prometheus and Grafana — on fresh volumes, with the models
loading from a read-only cache under `HF_HUB_OFFLINE=1`.

| Check | Result |
|---|---|
| `backend` health check (plain HTTP, `localhost`) | healthy before nginx started |
| `/health/`, `/ready/` over TLS | healthy; database, cache and broker `ok` |
| `http://` | 301 to `https://` |
| A deep link (`/documents/12`) | the application shell, with the CSP, `nosniff`, `DENY`, `Permissions-Policy`, COOP and `no-cache` |
| A hashed asset | `max-age=31536000, immutable` |
| Anonymous `GET /api/documents/` | 401 |
| `/media/…` | the application shell — no file is reachable |
| `/metrics` at the edge | 404; Prometheus scrapes the replica directly and reports the target `up` |
| Application containers | uid 10001; source not writable; no compiler in the image |
| Browser suite (`platform.spec.ts`) through nginx over TLS | **7/7** — upload, OCR of a scanned image, search, cited answers, follow-up, abstention, department boundary, administration, audit |
| `csp.spec.ts` — every screen and every preview | **no CSP violation**, and a deliberately injected inline script was refused |

Three production defects were found by this run and fixed (with regression checks in
`check_deployment_config.py` or the test suite):

1. nginx never loaded the project's configuration: a `command:` wrapper stopped the image
   from rendering its template, so the stock default server answered and nothing listened
   on 443.
2. Every Prometheus scrape was rejected: scrapes arrive by container IP over plain HTTP,
   which failed host validation (400) and would then have been redirected to HTTPS.
   `/metrics`, `/health/` and `/ready/` are now served ahead of both checks.
3. `/metrics` was reachable from outside, because traffic through Docker Desktop's port
   forwarding arrives from the (private) gateway address. It is no longer served at the
   edge at all.

The same checks, against a real host, are what the CI `smoke` job does once
`STAGING_URL` is set.

```bash
curl -fsS https://<host>/health/
curl -fsS https://<host>/ready/
curl -s -o /dev/null -w '%{http_code}' https://<host>/api/documents/   # expect 401
curl -sI https://<host>/ | grep -i content-security-policy
```
