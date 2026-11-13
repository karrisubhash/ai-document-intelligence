# Production deployment

Single host, Docker Compose, two files merged:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## What runs

| Service | Image | Role |
|---|---|---|
| `nginx` | built from `infrastructure/nginx/Dockerfile` | TLS, the compiled frontend, API proxy, edge rate limits, security headers. The only service published on the host's network interfaces (80, 443); Grafana listens on host loopback only |
| `backend` | built from `Dockerfile` | gunicorn + Django; warms models before taking traffic |
| `worker` | same | Celery, queue `ingest` (the pipeline) |
| `worker-index` | same | Celery, queue `index` (bulk re-embedding) |
| `migrate` | same | runs `migrate` once; `backend` starts only after it succeeds |
| `db` | `pgvector/pgvector:pg16` | PostgreSQL with pgvector; not published |
| `redis` | `redis:7-alpine` | broker, cache, sessions; AOF persistence; not published |
| `certbot` | `certbot/certbot` | certificate renewal every 12 h |
| `prometheus`, `grafana` | pinned versions | metrics; Grafana on 127.0.0.1:3000 only |

Application containers run as the unprivileged `app` user (uid 10001). Their images carry
no compiler, no source bind mounts, and — through `.dockerignore` — no uploaded documents,
database files, `.env` or virtualenvs.

`python infrastructure/check_deployment_config.py` asserts the **merged** configuration:
no writable source mount reaches the application containers; the backend, the workers, the
database and Redis publish no ports and nginx publishes 80 and 443 (Prometheus, Grafana and
certbot are not checked - Grafana is published on host loopback, 127.0.0.1:3000);
`DJANGO_DEBUG=False` and the deployment's settings reach every application service; the
application containers are not root and their tmpfs mounts survive a restart; and the edge
is the image with the frontend. CI runs
it; run it after editing either compose file.

## Before the first boot

1. **Configuration** in `.env` (never committed):

   | Variable | Required | Notes |
   |---|---|---|
   | `DJANGO_SECRET_KEY` | yes | ≥ 50 random characters. With DEBUG off the app refuses to start without one, or with `change-me` from `.env.example`. It does not check length or randomness |
   | `DJANGO_ALLOWED_HOSTS` | yes | the public hostname |
   | `CSRF_TRUSTED_ORIGINS` | yes | `https://<hostname>` |
   | `POSTGRES_PASSWORD` | yes | |
   | `NGINX_SERVER_NAME` | yes | the public hostname; also selects the certificate path |
   | `GRAFANA_PASSWORD` | yes | compose refuses to start without it |
   | `EMAIL_HOST` … | recommended | nothing in the application sends mail yet (passwords are reset by an administrator in the app, and alerts are not mailed). Django's `check --deploy` flags the console backend used without it |
   | `SECURE_HSTS_SECONDS` | decide | off by default because it is near-irreversible; `31536000` once TLS is stable |
   | `DOCUMENT_EMBEDDING_PROVIDER`, `RAG_RERANKER`, `RAG_ANSWERER`, `DOCUMENT_SCANNER`, … | optional | see [ai/pipeline.md](../ai/pipeline.md) |
   | `AWS_STORAGE_BUCKET_NAME` … | optional | S3-compatible object storage instead of the `media_data` volume. Build with `INSTALL_S3=true`, or the first file operation fails. Unverified against a real bucket: see [release/multi-host-validation.md](../release/multi-host-validation.md) |

2. **Ownership of bind-mounted directories.** The containers write `./backups` (mounted
   at `/backups`, which is `BACKUP_ROOT` in production) as uid 10001:
   `sudo mkdir -p backups && sudo chown 10001:10001 backups`. Volumes created by an
   image older than the unprivileged user are root-owned; fix once with
   `docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm -u root backend
   chown -R app:app /app/backend/media /app/backend/staticfiles /app/.cache`.

3. **TLS.** For a public hostname, `infrastructure/init-letsencrypt.sh` writes a temporary
   self-signed pair, starts nginx, removes the placeholder, obtains the real certificate
   over HTTP-01 and reloads (if issuance fails, a placeholder is put back so nginx can
   still restart). Run it with `STAGING=1` first.
   It needs `NGINX_SERVER_NAME` and `CERTBOT_EMAIL`, and ports 80/443 reachable from the
   internet. Behind an institutional load balancer that terminates TLS instead, place its
   certificate at `/etc/letsencrypt/live/<hostname>/` in the `letsencrypt` volume.

4. **Models**, if local embeddings or reranking are configured:
   `docker compose … run --rm backend python manage.py warm_models` downloads the weights
   into the `hf_cache` volume once. Afterwards set `HF_HUB_OFFLINE=1` to guarantee that no
   model code contacts the Hugging Face Hub at runtime.

5. **First administrator:** `docker compose … run --rm backend python manage.py
   createsuperuser`, then sign in, create departments, then accounts.

## Upgrades

```bash
git pull
python infrastructure/check_deployment_config.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
# --no-deps: without it, `run` starts `migrate` first (backend depends on it), and the
# "pre-upgrade" backup would be taken AFTER the new migrations had run.
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend python manage.py backup --label pre-upgrade
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`migrate` runs before `backend` and both workers start, so new task code never runs
against the old schema. A change of embedding model needs `manage.py reindex` afterwards
(it runs on the `index` queue, beside live traffic).

**The stack survives restarts.** Containers restart in place after a crash, a Docker
daemon restart or a host reboot. Every tmpfs an unprivileged service writes declares its
owner and mode, because Docker remounts a default tmpfs root-owned on every restart after
the first start. That once left the backend crash-looping after a reboot.
`check_deployment_config.py` rejects an application-service tmpfs that declares neither a
`uid=` nor a `mode=` (either one is enough to keep it writable).

## Small-server profile

For a 2 vCPU / 8 GB CPU-only host (the live server's size), add the third file:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.small.yml up -d
# with Prometheus and Grafana:
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.small.yml --profile monitoring up -d
python infrastructure/check_deployment_config.py docker-compose.small.yml
```

It writes down the tuning the live server used to set by hand in `.env`. Every
value is a default that `.env` can still override.

| Setting | Small profile | Production overlay | Why |
|---|---|---|---|
| `GUNICORN_WORKERS` × `GUNICORN_THREADS` | 2 × 4 | 3 × 2 | two copies of the local models in memory; eight request slots, since a streamed chat answer holds one for its whole length |
| `CELERY_CONCURRENCY` (ingest) | 1 | 2 | OCR is CPU-bound; two OCR processes would take both cores from searches and answers |
| `CELERY_INDEX_CONCURRENCY` | 1 | 6 | bulk re-embedding is rare; six processes idle at 2.3 GiB |
| `EMBEDDING_LOCAL_THREADS` | 1 (backend and both workers) | 0 (torch's choice: one per host core) | processes do not fight over two cores |
| `OMP_THREAD_LIMIT` (ingest worker) | 1 | unset | tesseract likewise |
| `mem_limit` | db 1g, redis 512m, backend 2.5g, worker 2g, worker-index 1.5g, nginx 256m, certbot 128m, prometheus 512m, grafana 256m | none | one runaway (a 500-page scan) is OOM-killed and restarted alone |
| `cpus` | worker 1.5, worker-index 1.0 | none | a long OCR run always leaves room for web requests |
| logs | json-file, 10 MB × 3 per container | Docker default (unbounded) | the live server set this in `/etc/docker/daemon.json` |
| Prometheus, Grafana | only with `--profile monitoring` | always | together ~0.5 GB a small box needs more; `GRAFANA_PASSWORD` is still required to parse the overlay |

Ceilings are not reservations: measured use is backend 1.2–1.7 GiB, ingest worker
about 1.1 GiB, Postgres about 150 MB (see
[release/staging-runbook.md](../release/staging-runbook.md)). Keep swap enabled
(the live server has 4 GiB). Still set `HF_HUB_OFFLINE=1` after `warm_models`.

## Compression

nginx gzips JavaScript, CSS, JSON, SVG and manifests (level 5, responses of 1 kB
or more, `Vary: Accept-Encoding`) - the built app is about 600 kB raw and 180 kB
gzipped. `text/event-stream` is deliberately not compressed: compression would
hold the chat stream's events back until a block filled. The stream also opts out
of proxy buffering with `X-Accel-Buffering: no` (see `infrastructure/nginx/proxy_params`).
Verified on `nginx:1.27-alpine`: `nginx -t` passes; assets, the app shell and API
JSON come back with `Content-Encoding: gzip`; a streamed response arrives event by
event, uncompressed. `npm run size` in `frontend/` reports the gzipped sizes.
## The landing page, the public demo and sample content

Signed-out visitors at `/` see the landing page, with the sign-in form in it; `/login`,
`/signup` and any other address show the focused sign-in. Nothing to configure, except
the site's address for link previews: build the edge image with
`--build-arg VITE_PUBLIC_URL=https://docs.example.edu` so `og:image` and `og:url` are
absolute (empty, the default, leaves them relative, which Facebook, LinkedIn and X ignore).
The share image, icons and `robots.txt` are in `frontend/public/`; `frontend/scripts/brand_assets.py`
renders the PNGs again if the mark changes.

**Sample content.** `manage.py seed_demo` loads the sample library - sixteen synthetic
institutional documents, three of them PDFs with tables and one a scan that only OCR reads -
into `DEMO_DEPARTMENT` (created if missing), through the real upload path, so the worker
must be running for them to become Ready. `--department NAME` fills another department
(created if missing). Safe to run again: what a department already holds, by checksum, is
skipped. A system administrator can do the same from the API for any department
(`POST /api/v1/admin/samples/`), which is how an empty installation gets something to
show. The samples are owned by an inactive `sample-library` account, made on first use
with no password. Owning a document grants reading and managing it in every department,
so the name is reserved (sign-up, account creation and approval refuse it, in any case
or width), and loading the samples refuses - `seed_demo` with an error, the API with
**409** - when an account that can sign in, or has a role or department, already holds
it: one made before the name was reserved, or in the Django admin. Rename or delete that
account there, then load the samples again.

**Turning the demo on** (off by default):

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend \
    python manage.py seed_demo
# wait until the samples are Ready (Documents, or `docker compose logs -f worker`), then in .env:
DEMO_ENABLED=True
# and recreate backend so it reads the setting
```

The landing page offers "Try the live demo" only while the demo department holds at least
one Ready document. Each visitor gets an ephemeral, read-only guest account
([security model](../security/model.md#identity)). Schedule the sweep - the TTL is only as
good as it:

```sh
# hourly, e.g. from the host's crontab
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend \
    python manage.py purge_demo_guests
```

| Setting | Default | Meaning |
|---|---|---|
| `DEMO_ENABLED` | `False` | Offer the one-click demo |
| `DEMO_DEPARTMENT` | `Demo` | Where guests live and `seed_demo` puts the samples |
| `DEMO_GUEST_TTL_HOURS` | `3` | Guests older than this are deleted; sessions end no later |
| `DEMO_GUEST_IDLE_MINUTES` | `30` | At the ceiling, a guest idle this long makes way for a new visitor (at least 10) |
| `DEMO_MAX_GUESTS` | `200` | Guest accounts existing at once; past it, with none idle, "the demo is full" |
| `THROTTLE_DEMO` / `THROTTLE_DEMO_GLOBAL` | `10/hour` / `60/hour` | Demo starts per address / from all addresses |
| `THROTTLE_GUEST_CHAT` / `_BURST` / `_GLOBAL` | `20/hour` / `5/minute` / `120/hour` | Questions per guest / per guest per minute / all guests |
| `THROTTLE_GUEST_SEARCH` / `_GLOBAL` | `60/hour` / `600/hour` | Searches per guest / all guests |
| `THROTTLE_SAMPLES` | `10/hour` | Sample-library loads per administrator |

**Keeping the ceiling out of reach.** Starting a demo takes nothing but a CSRF cookie, and
the per-address limit stops nobody with a few addresses, so `DEMO_MAX_GUESTS` must not be
reachable by guests nobody uses - or a script starting demos and walking away could keep
every visitor out. Two things see to it. At the ceiling, a new visitor takes the place of
the guest that has gone longest without a request, once that is `DEMO_GUEST_IDLE_MINUTES`
(counted from the demo's start for a guest that never made one; audited as a
`GUEST_PURGE` with `reason: "idle"`); below it nobody is displaced. And at most
`THROTTLE_DEMO_GLOBAL` × `DEMO_GUEST_TTL_HOURS` demos can start before the first expires -
60/hour × 3 h = 180 with the defaults - which is within the 200 guests allowed, so not even
guests kept busy can fill it. Raise one of these and keep that product within
`DEMO_MAX_GUESTS`: `manage.py check` (and `migrate`) warns when it is not (`accounts.W001`),
and when even guests nobody uses could fill it within the idle time (`accounts.W002`).

On a server whose answerer is a hosted model with a daily quota, guests' questions draw on
the same quota as members'; `THROTTLE_GUEST_CHAT_GLOBAL` is what bounds their share. Lower
it, or leave the demo off, if the quota is tight.

## Scaling

`docker compose … up -d --scale backend=3` — nginx balances by least connections across
replicas, re-resolving the `backend` name every 10 s (`resolve` on the upstream; nginx
1.27.3 or later), so added or recreated replicas are picked up without a reload. Scale `worker` for upload throughput and `worker-index` for re-indexing
independently. All of this is **one host**: `--scale` never places a container on another
machine.

More than one host needs object storage (the `media_data` volume is local to one
machine), a shared Postgres and Redis that other hosts can reach (with a Redis password),
a load balancer in front of the nginx instances, and Prometheus targets for the remote
workers. This overlay provides none of them, and the deployment check rejects publishing
the database or Redis. See [release/multi-host-validation.md](../release/multi-host-validation.md).

## Dependencies

Every Python install, in the image and in CI, runs against `backend/constraints.txt`:
the exact versions the tested image contained. A rebuild months later installs the same
packages. To upgrade one, change its pin, rebuild, run the full suite and both evaluation
gates, then commit the pin with the result. The base image (`python:3.12-slim`), the
Debian packages and the `certbot/certbot:latest` and `nginx:1.27-alpine` tags are not
pinned. Record their digests at staging and pin production to them.

## Verification status

| Item | Status |
|---|---|
| Merged configuration passes the deployment check | Verified locally |
| Web image builds, with frontend typecheck and unit tests inside the build | Verified locally |
| Full production stack boots, serves the SPA over TLS, and the API works behind it | **Verified locally**: browser suite 7/7 through nginx over TLS, no CSP violations, metrics scraped — see [operations/runbook.md](../operations/runbook.md#production-stack-smoke-test) |
| Image built from `constraints.txt` with every optional layer (ML, LLM, local models, S3) | **Verified locally**: installed packages identical to the constraints file (90 of 90); the S3 backend, boto3, torch and both provider SDKs import; runs as uid 10001 |
| Let's Encrypt issuance and renewal | **Unverified** — needs a public hostname. Test plan: [release/staging-runbook.md](../release/staging-runbook.md#tls-test-plan) |
| S3 / MinIO storage | Contract-tested with a stub; **unverified against a real bucket** |
| Multi-host operation | **Unverified** |
