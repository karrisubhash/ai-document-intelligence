# Staging deployment runbook

How to take this repository from a checkout to a running staging host with a real
certificate, and how to prove each step worked. **None of this has been run on a real
server yet.** Everything below the "verified locally" notes is a procedure, not a record.
Record each result in the staging log (step 0).

The production procedure is the same with a production hostname and production secrets.
Production should follow only after staging has passed every step here.

**Rehearsed locally (2026-10-06)** on the release-candidate images, with a self-signed
certificate for `localhost`, a disposable database and the recommended pilot configuration:

- steps 13 and 15–17: both workers reply on both queues, all 4 Prometheus targets are up,
  the backup is verified, and the restore into a disposable database matches row for row;
- step 18: a rollback across a migration, using the commands below verbatim;
- step 19: the edge checks and the browser suite, 8/8.

Everything that needs a real server, DNS or a public certificate has not been run.

Every command runs on the staging host from the repository root. To keep commands short,
define:

```bash
export DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
```

## 0. Inputs you need before starting

| Input | Who provides it | Used in |
|---|---|---|
| A Linux x86_64 server you control | infrastructure owner | 1 |
| A public hostname for staging, e.g. `docs-staging.<institution-domain>` | DNS owner | 3, 11 |
| An email address for certificate expiry notices | operations | 11 |
| An SMTP relay (host, port, user, password) | IT | 5 |
| The release commit or tag to deploy | release owner | 1 |
| A place to copy backups off the host | infrastructure owner | 16 |

Start a staging log (a plain text file or ticket). Record the commit, every command's
result, and every deviation from this page.

## 1. Server

| Requirement | Value | Basis |
|---|---|---|
| Architecture | x86_64 | the images install the CPU build of torch for x86_64; arm64 has never been built |
| CPU | 4 vCPU minimum, 8 recommended | **measured**: the 3,000-document test ran in a Docker VM with 8 CPUs; the ingest worker used about 5.3 cores |
| Memory | 8 GiB minimum | **measured** with the pilot configuration: backend 1.2–1.7 GiB, ingest worker about 1.1 GiB, index worker 2.3 GiB idle at its default concurrency of 6 (set `CELERY_INDEX_CONCURRENCY=1`, step 5), Postgres about 150 MB, inside a 7.7 GiB VM |
| Disk | 40 GB plus the expected size of the original uploads, multiplied by the backup retention | **estimated**: about 2.5 GB of images, about 1.5 GB of model weights, 86 MB of database for 3,000 small text documents |
| OS | any current Linux with Docker Engine support | not tested on a server; development used Docker Desktop on Windows |

```bash
git clone <repository> ai-document-intelligence && cd ai-document-intelligence
git checkout <release-tag>
git log -1 --format='%H %s'      # record this in the staging log
```

## 2. Docker

You need Docker Engine and the Compose plugin **2.24.4 or later**. The production overlay
uses the `!override` merge tag; older Compose versions do not understand it and silently
publish the database. The stack was tested with Compose v5.5.1.

```bash
docker compose version
python3 infrastructure/check_deployment_config.py    # must print every check as passing
```

`check_deployment_config.py` checks the configuration after the two compose files are
merged:

- no service except nginx publishes a port;
- no source directory is mounted into a container;
- `DEBUG` is off;
- the containers do not run as root;
- tmpfs mounts survive a restart.

Run it after every edit to either compose file.

## 3. DNS

Create an **A** record from the staging hostname to the server's public IPv4 address.

```bash
dig +short A docs-staging.<institution-domain>       # must print the server's address
dig +short AAAA docs-staging.<institution-domain>    # see below
```

nginx listens with `listen 80;` and `listen 443 ssl;`, which means IPv4 only inside the
container. Let's Encrypt validates over IPv6 when an AAAA record exists. So publish an AAAA
record only after checking that `curl -6 http://<host>/.well-known/acme-challenge/probe`
reaches nginx and returns nginx's 404. Otherwise leave AAAA out.

## 4. Firewall

| Port | Source | Why |
|---|---|---|
| 22 | administrators' addresses only | SSH, and the tunnel to Grafana |
| 80 | anywhere | the ACME HTTP-01 challenge and certificate renewal; all other requests are redirected to HTTPS |
| 443 | the intended users (anywhere, or the institution's ranges) | the application |
| everything else | closed | |

Docker publishes ports through its own iptables chain, which **bypasses `ufw` and
firewalld rules on the host**. The overlay publishes only 80 and 443, and Grafana is bound
to 127.0.0.1:3000, so this does not expose anything else. Check from a machine outside the
network:

```bash
nmap -Pn -p- docs-staging.<institution-domain>    # expect only 22, 80, 443 open
```

If 443 must be limited to institutional ranges, do it at the cloud or network firewall,
not with `ufw`.

## 5. Environment variables

Create `.env` in the repository root. It is gitignored and the image build excludes it
(`.dockerignore`). The variable names below are the ones the code reads.

```bash
# Identity of the deployment
COMPOSE_PROJECT_NAME=adi                 # predictable image and volume names (adi-backend, adi_postgres_data)
DJANGO_ALLOWED_HOSTS=docs-staging.<institution-domain>
CSRF_TRUSTED_ORIGINS=https://docs-staging.<institution-domain>
NGINX_SERVER_NAME=docs-staging.<institution-domain>

# Secrets (step 6)
DJANGO_SECRET_KEY=
POSTGRES_PASSWORD=
GRAFANA_PASSWORD=

# Database and broker: the compose service names
POSTGRES_DB=adi
POSTGRES_USER=adi
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_HOST=redis
REDIS_PORT=6379
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/1
APP_PORT=8000

# Mail. Nothing in the application sends mail yet; without a relay, Django's
# `check --deploy` flags the console backend.
EMAIL_HOST=
EMAIL_PORT=587
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=
EMAIL_USE_TLS=True

# AI: the recommended pilot configuration (RELEASE_READINESS.md)
DOCUMENT_EMBEDDING_PROVIDER=documents.processing.embedding.local.LocalEmbeddingProvider
RAG_RERANKER=documents.rag.local_rerank.LocalCrossEncoderReranker
RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2
# RAG_ANSWERER stays unset: the extractive answerer, no external calls.

# The re-index worker defaults to 6 processes (sized for a hosted embedder).
# With local models each one loads the model: 2.3 GiB measured while idle.
CELERY_INDEX_CONCURRENCY=1

# Storage: the local media volume (single host). Build without the S3 layer.
INSTALL_S3=false

# Backups
BACKUP_RETENTION=7
```

**Check the AI class paths** before the first boot. The reranker's class name above must
match the one in [ai/pipeline.md](../ai/pipeline.md) and `documents/rag/`. If a path is
wrong, the import fails when the component is first used:

```bash
$DC run --rm --no-deps backend python manage.py shell -c "
from django.conf import settings
from django.utils.module_loading import import_string
for name in ('DOCUMENT_EMBEDDING_PROVIDER', 'RAG_RERANKER'):
    print(name, import_string(getattr(settings, name)))"
```

Do **not** set `SECURE_HSTS_SECONDS` on staging until the real certificate has renewed at
least once. HSTS is close to irreversible, so it is a decision for the domain owner.

## 6. Secrets

| Secret | How to generate | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` | The app refuses to start without one when DEBUG is off. Changing it signs every user out |
| `POSTGRES_PASSWORD` | `openssl rand -base64 32` | **Read only when the database volume is first created.** Changing it later means running `ALTER USER` inside Postgres, then updating `.env` |
| `GRAFANA_PASSWORD` | `openssl rand -base64 24` | Compose refuses to start without it |
| `EMAIL_HOST_PASSWORD` | from IT | |
| Provider API keys | only if a hosted answerer is approved ([ai-release.md](ai-release.md#hosted-answerer-activation)) | none for the pilot |

```bash
chmod 600 .env && ls -l .env      # -rw------- owned by the deploying user
```

Use a different value for every secret on staging and production. CI uses no secrets
today, and none of these values should ever be added to the repository or to CI.

## 7. Database

PostgreSQL 16 with pgvector, from the `pgvector/pgvector:pg16` image. It is not published,
and its data is in the `adi_postgres_data` volume.

```bash
$DC up -d db
$DC exec db pg_isready -U adi                  # "accepting connections"
```

After migrations (step 12):

```bash
$DC exec db psql -U adi -d adi -c "select extname, extversion from pg_extension where extname = 'vector';"
```

## 8. Redis

`redis:7-alpine`, with append-only persistence in a volume. It is not published and has
**no password**. That is acceptable only while Redis is reachable from the compose network
alone; see the [security gaps](RELEASE_READINESS.md#security-gaps).

```bash
$DC up -d redis
$DC exec redis redis-cli ping                  # PONG
```

## 9. Object storage

**For the pilot, use the local `media_data` volume.** Uploads then live on this host and go
into the same backups as the database (step 16).

S3 or MinIO is only needed for more than one application host. It requires building with
`INSTALL_S3=true` and setting `AWS_STORAGE_BUCKET_NAME` and the other `AWS_*` variables.
It has never been tested against a real bucket. Before using it, run the validation in
[multi-host-validation.md](multi-host-validation.md).

## 10. Build, and nginx

```bash
$DC build                                      # web edge (with frontend tests inside), backend, workers
docker image ls 'adi-*'
```

nginx renders `infrastructure/nginx/nginx.conf` from a template with `NGINX_SERVER_NAME`
when it starts. It serves the compiled frontend, proxies `/api/`, sets the security
headers, and applies the edge rate limits.

After step 11:

```bash
$DC exec nginx nginx -T 2>/dev/null | grep -E 'server_name|ssl_certificate '
```

## 11. TLS

Follow the [TLS test plan](#tls-test-plan) below. In short:

```bash
STAGING=1 NGINX_SERVER_NAME=docs-staging.<institution-domain> CERTBOT_EMAIL=<ops-email> \
  sh infrastructure/init-letsencrypt.sh        # Let's Encrypt's staging CA first
NGINX_SERVER_NAME=docs-staging.<institution-domain> CERTBOT_EMAIL=<ops-email> \
  sh infrastructure/init-letsencrypt.sh        # then the real certificate
```

`init-letsencrypt.sh` runs `up -d nginx`. nginx depends on a healthy backend, so this step
starts the whole application. `.env` must therefore be complete before you run it.

## 12. Migrations

The `migrate` service runs `migrate --noinput` once. `backend` and both workers start only
after it exits successfully.

```bash
$DC up -d
$DC ps -a migrate                               # Exited (0)
$DC run --rm --no-deps backend python manage.py showmigrations --plan | grep -c '\[ \]'    # 0
```

Create the first administrator:

```bash
$DC run --rm --no-deps backend python manage.py createsuperuser
```

## 13. Workers and models

`worker` consumes the `ingest` queue (the pipeline). `worker-index` consumes `index` (bulk
re-embedding).

```bash
$DC run --rm --no-deps backend python manage.py warm_models    # downloads into the hf_cache volume once
$DC exec worker celery -A config inspect ping                   # both workers reply
$DC exec worker celery -A config inspect active_queues | grep -E "'name': '(ingest|index)'"
```

After `warm_models` succeeds, add `HF_HUB_OFFLINE=1` to `.env` and run `$DC up -d`. From
then on, no model code contacts the Hugging Face Hub at runtime.

Schedule the stalled-job sweep on the host. Nothing in the stack schedules it:

```cron
*/15 * * * * cd /srv/ai-document-intelligence && docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend python manage.py recover_stalled_jobs --apply --requeue >> /var/log/adi-stalled.log 2>&1
```

## 14. Health checks

```bash
curl -fsS https://docs-staging.<institution-domain>/health/    # {"status": "healthy", ...}
curl -fsS https://docs-staging.<institution-domain>/ready/     # database, cache, broker all "ok"
$DC ps                                                          # backend "healthy"; nothing restarting
```

Sign in as the administrator. Open **System status** (`/admin/system`). It reports storage
write/read, queue depths, stalled runs, recent failures and the active AI configuration.
The AI configuration must show the step 5 values: local embeddings, the MiniLM
cross-encoder and the extractive answerer.

`/ready/` does not check storage. A broken storage backend shows up only on System status.

## 15. Monitoring

Prometheus scrapes the backend (:8000) and both workers (:9100) inside the compose network.
It is not published. Grafana listens on the host's loopback only:

```bash
ssh -L 3000:localhost:3000 <staging-host>      # then http://localhost:3000, user admin, GRAFANA_PASSWORD
$DC exec prometheus wget -qO- 'http://localhost:9090/api/v1/targets' | grep -o '"health":"[a-z]*"' | sort | uniq -c
#   expect 4 "up": adi-backend, worker, worker-index, prometheus
```

The Prometheus datasource is provisioned. **No dashboards and no alert rules ship, and
nothing delivers alerts.** Build the panels in
[post-go-live monitoring](RELEASE_READINESS.md#post-go-live-monitoring) before go-live.
Choosing where alerts go is an institutional decision.

Uptime from outside: point the institution's external monitor at `/health/`. Nothing in
this repository watches the host from outside.

## 16. Backups

```bash
sudo install -d -o 10001 -g 10001 backups
$DC run --rm --no-deps backend python manage.py backup --include-media --label first
ls -l backups/                                  # db-<stamp>-first.dump, media archive, manifest
```

Host cron, daily:

```cron
30 2 * * * cd /srv/ai-document-intelligence && docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps backend python manage.py backup --include-media --prune >> /var/log/adi-backup.log 2>&1
```

**Off-host copy.** A backup on the same disk protects against mistakes, not against losing
the disk. Copy `backups/` to the off-host location from step 0, encrypted. The dumps
contain password hashes and the full text of every document. For example:

```cron
0 4 * * * tar -C /srv/ai-document-intelligence -cf - backups | age -r <recipient-public-key> > /mnt/offhost/adi-$(date +\%F).tar.age
```

The tool (`age`, `gpg`, the backup system's own encryption), who holds the key, and how
long copies are kept are institutional decisions. Nothing in this repository encrypts or
ships backups off the host.

## 17. Restore verification

Restore the latest dump into a **separate, disposable database** on the staging host.
Never restore into the live database to test a backup.

```bash
STAMP=<stamp of the dump to test>
$DC exec db createdb -U adi adi_restore_check
$DC run --rm --no-deps -e POSTGRES_DB=adi_restore_check backend \
  python manage.py restore /backups/db-$STAMP.dump --yes        # checksum OK, tables present
for DB in adi adi_restore_check; do
  echo "== $DB"; $DC exec db psql -U adi -d $DB -Atc \
    "select 'documents', count(*) from documents_document union all
     select 'versions', count(*) from documents_documentversion union all
     select 'chunks', count(*) from documents_documentchunk union all
     select 'embeddings', count(*) from documents_documentchunkembedding union all
     select 'audit', count(*) from documents_auditlog union all
     select 'users', count(*) from auth_user;"
done                                                              # counts must match
$DC exec db dropdb -U adi adi_restore_check
```

Time the restore and record it. **No restore time has been measured beyond a 157 KB
dump.**

The media archive is a tarball of the `media_data` volume. To check it, list it
(`tar -tzf`) and compare the file count with the volume. A full media restore means
stopping the stack and extracting into the volume.

## 18. Rollback

Before every upgrade, keep the running images and take a backup:

```bash
for s in backend worker worker-index migrate nginx; do docker tag adi-$s:latest adi-$s:rollback; done
$DC run --rm --no-deps backend python manage.py backup --include-media --label pre-upgrade
git rev-parse HEAD > .release-previous
```

Then upgrade (`git checkout <new-tag>`, `$DC build`, `$DC up -d`). To roll back:

```bash
$DC stop nginx backend worker worker-index
for s in backend worker worker-index migrate nginx; do docker tag adi-$s:rollback adi-$s:latest; done
git checkout "$(cat .release-previous)"
```

- **If the new release ran no migrations,** start the old images: `$DC up -d --no-build`.
- **If it did,** the old code may not run on the new schema. Restore the pre-upgrade dump
  into the live database, then start:

  ```bash
  $DC run --rm --no-deps backend python manage.py restore /backups/db-<stamp>-pre-upgrade.dump --yes
  $DC up -d --no-build
  ```

  Anything written between the upgrade and the rollback is lost. That includes new
  documents, chat messages and audit rows. Record the window in the staging log.

If the tagged images are gone, rebuild the previous tag. `backend/constraints.txt` pins
every Python package, so the rebuild installs the same versions. The base image
(`python:3.12-slim`) and the Debian packages are not pinned and may differ.

Rehearse a rollback on staging once, from this release to the previous one, before
production depends on it.

## 19. Smoke tests

The automated subset. This is what the CI `smoke` job runs once the GitHub variable
`STAGING_URL` is set:

```bash
BASE=https://docs-staging.<institution-domain>
curl -fsS "$BASE/health/" | grep -q '"status": "healthy"' && echo health-ok
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/documents/"); echo "anonymous documents: $code"   # 401 or 403
```

Manual, and record each result:

| Check | Expect |
|---|---|
| `curl -sI http://$HOST/` | `301` to `https://` |
| `curl -sI $BASE/` | `content-security-policy`, `x-frame-options: DENY`, `x-content-type-options: nosniff` |
| `curl -s -o /dev/null -w '%{http_code}' $BASE/metrics` | `404` (metrics are not served at the edge) |
| `curl -s $BASE/media/anything` | the application shell, never a file |
| Sign in as the administrator; create a department and two accounts in different departments; reset one account's password from the admin screens | works; the account signs in with the new password, and its old sessions are gone |
| Upload a **public, non-sensitive** test PDF (for example a published regulation) as one account | reaches *Ready*; the timeline shows each stage |
| Search for a phrase in it, then ask a question it answers | the document is found; the answer cites it and the citation opens the right page |
| Ask a question it does not answer | the system declines rather than guessing |
| Sign in as the account from the other department and search for the same phrase | **not found**: the department boundary holds |
| Audit log (administrator) | the upload, the search and the denied access are recorded |
| `$DC restart backend worker` | both healthy again, with no crash loop |
| Delete the test document | gone from search |

The browser suite can also run against staging. Only do this if staging's database is
disposable, because `seed_e2e` creates four test accounts and the suite uploads documents:

```bash
$DC exec -T backend python manage.py seed_e2e --password "$E2E_PASSWORD"
cd frontend && E2E_BASE_URL=$BASE E2E_PASSWORD=... npx playwright test
```

---

## TLS test plan

The bootstrap logic was reviewed and corrected, but no certificate has ever been issued
from this repository. This plan is how to prove it works. Stop at the first failure.

| # | Test | Command | Pass |
|---|---|---|---|
| T1 | DNS resolves to this host | `dig +short A $HOST` | the server's public address |
| T2 | Port 80 reachable from outside | from another network: `curl -s -o /dev/null -w '%{http_code}' http://$HOST/.well-known/acme-challenge/probe` | `404` **from nginx** (after T4); a timeout means a firewall or DNS problem |
| T3 | Port 443 reachable from outside | `nc -vz $HOST 443` from another network | connected |
| T4 | Placeholder lets nginx start | `STAGING=1 … init-letsencrypt.sh`, step 1/3 | nginx starts; `curl -kI https://$HOST/` answers with the self-signed certificate |
| T5 | ACME challenge served | write a probe file: `$DC run --rm --entrypoint sh certbot -c 'mkdir -p /var/www/certbot/.well-known/acme-challenge && echo ok > /var/www/certbot/.well-known/acme-challenge/probe'`, then `curl http://$HOST/.well-known/acme-challenge/probe` from outside | `ok` (served over plain HTTP, **not** redirected to HTTPS) |
| T6 | Staging-CA issuance | the rest of `STAGING=1 … init-letsencrypt.sh` | "Reloading nginx with the issued certificate"; `echo \| openssl s_client -connect $HOST:443 -servername $HOST 2>/dev/null \| openssl x509 -noout -issuer` names the Let's Encrypt **staging** issuer |
| T7 | Real issuance | `init-letsencrypt.sh` without `STAGING` | the issuer is a production Let's Encrypt intermediate; `curl -sSI https://$HOST/health/` works **without** `-k` |
| T8 | Renewal works | `$DC run --rm --entrypoint certbot certbot renew --dry-run --webroot --webroot-path=/var/www/certbot` | "Congratulations, all simulated renewals succeeded" |
| T9 | The renewal loop is alive | `$DC logs --since 13h certbot` | a `certbot renew` run at least every 12 h, ending in "No renewals were attempted" or a successful renewal |
| T10 | nginx picks up a renewed certificate | `$DC exec nginx nginx -s reload`, then compare `openssl x509 -noout -enddate` before and after a forced renewal (`certbot renew --force-renewal` against the **staging** CA only) | the new expiry date is served without restarting nginx. The automatic reload runs every 6 h (`40-reload-certificates.sh`) |
| T11 | Expiry is watched | set an external monitor to alert 14 days before expiry | the institution's monitor shows the certificate |

**Failure recovery.**

| Failure | What you see | What to do |
|---|---|---|
| Issuance fails (DNS, firewall, rate limit) | the script prints "certbot failed - a temporary self-signed certificate is in place" and exits 1 | nginx still restarts on the placeholder. Fix the cause using T1–T5, then re-run with `STAGING=1`. **Do not retry the production CA in a loop**: Let's Encrypt limits failed validations per hostname per hour and duplicate certificates per week |
| The script dies between removing the placeholder and issuance | `nginx -t` fails: no certificate at `/etc/letsencrypt/live/$HOST/` | run the script again. Step 1 writes a new placeholder when none exists |
| Renewal fails repeatedly | T9 shows errors; Let's Encrypt emails `CERTBOT_EMAIL` as expiry approaches | certificates are valid for 90 days and renewal starts 30 days before expiry, so there are weeks to fix it. Check that port 80 is still open and that the challenge location still bypasses the HTTPS redirect (T5) |
| The certificate renewed but browsers still see the old one | the expiry date has not changed | `$DC exec nginx nginx -s reload` |
| TLS terminates at an institutional load balancer instead | | skip `init-letsencrypt.sh`. Place the institution's certificate and key at `/etc/letsencrypt/live/$HOST/fullchain.pem` and `privkey.pem` in the `letsencrypt` volume. Stop the `certbot` service. Check `DRF_NUM_PROXIES` and the forwarded-proto handling ([multi-host-validation.md](multi-host-validation.md#edge-behind-another-load-balancer)) |

The `certbot` service uses `certbot/certbot:latest`, and nginx is built from
`nginx:1.27-alpine`. Both tags float. Record the image digests staging passed with
(`docker image inspect --format '{{index .RepoDigests 0}}' certbot/certbot:latest`). Pin
production to those digests.
