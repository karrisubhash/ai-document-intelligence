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
   | `DJANGO_SECRET_KEY` | yes | ≥ 50 random characters. The app refuses to start without one when DEBUG is off |
   | `DJANGO_ALLOWED_HOSTS` | yes | the public hostname |
   | `CSRF_TRUSTED_ORIGINS` | yes | `https://<hostname>` |
   | `POSTGRES_PASSWORD` | yes | |
   | `NGINX_SERVER_NAME` | yes | the public hostname; also selects the certificate path |
   | `GRAFANA_PASSWORD` | yes | compose refuses to start without it |
   | `EMAIL_HOST` … | recommended | password resets and alerts; the deploy check flags a console mail backend |
   | `SECURE_HSTS_SECONDS` | decide | off by default because it is near-irreversible; `31536000` once TLS is stable |
   | `DOCUMENT_EMBEDDING_PROVIDER`, `RAG_RERANKER`, `RAG_ANSWERER`, `DOCUMENT_SCANNER`, … | optional | see [ai/pipeline.md](../ai/pipeline.md) |
   | `AWS_STORAGE_BUCKET_NAME` … | optional | S3-compatible object storage instead of the `media_data` volume |

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

## Scaling

`docker compose … up -d --scale backend=3` — nginx balances by least connections across
replicas, re-resolving the `backend` name every 10 s (`resolve` on the upstream; nginx
1.27.3 or later), so added or recreated replicas are picked up without a reload. Scale `worker` for upload throughput and `worker-index` for re-indexing
independently. More than one host requires object storage (`AWS_STORAGE_BUCKET_NAME`),
because the `media_data` volume is local to one machine.

## Verification status

| Item | Status |
|---|---|
| Merged configuration passes the deployment check | Verified locally |
| Web image builds, with frontend typecheck and unit tests inside the build | Verified locally |
| Full production stack boots, serves the SPA over TLS, and the API works behind it | **Verified locally**: browser suite 7/7 through nginx over TLS, no CSP violations, metrics scraped — see [operations/runbook.md](../operations/runbook.md#production-stack-smoke-test) |
| Let's Encrypt issuance and renewal | **Unverified** — needs a public hostname |
| S3 / MinIO storage | Contract-tested with a stub; **unverified against a real bucket** |
| Multi-host operation | **Unverified** |
