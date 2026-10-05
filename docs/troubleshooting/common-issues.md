# Troubleshooting

Symptoms seen in practice on this codebase, with their causes.

## The first search after a restart takes 10–30 seconds

**Cause.** The local embedding model loads on first use in each process. Before a fix, it
also made ~21 requests to huggingface.co on every load even with the weights cached, and
the first search measured 30.5 s.

**Now.** Models load from the cache without network access, and gunicorn/Celery workers
load them at start. Under `runserver` (development) the first query in the process still
pays the ~11 s load. In production, run `manage.py warm_models` after deploying and set
`HF_HUB_OFFLINE=1`.

## Search returns results but the note says "lexical results only"

The embedding provider could not be loaded or could not embed the query (missing
`sentence-transformers`, an empty model cache under `HF_HUB_OFFLINE=1`, a model of the wrong
width). Search degrades rather than failing. The system status screen shows the configured
provider; `manage.py warm_models` reports the underlying error.

## "No passage survived context construction" on a short document

Passages below a minimum length are dropped from the context on purpose — a fragment is
not evidence. A one-line document produces a one-line chunk. This is most often seen with
hand-made test fixtures.

## A follow-up question gets the previous answer again

Fixed: follow-ups now carry the user's own words as the answer's focus. If it recurs, the
line above the answer ("Read as a follow-up: searched for ...") shows the resolved query, which
is also stored in the message's `metadata.followup`; check which terms were borrowed. `RAG_FOLLOWUP_HISTORY_TURNS=0` disables resolution entirely.

## An answer abstains with "could not be grounded"

A generative answerer produced text that failed verification — usually a figure that is not
in any cited passage. This is the guardrail working. The grounding failures are stored with
the message (`metadata.grounding_failures`) and counted in
`adi_rag_abstentions_total{reason=...}`.

## Answers are "Quoted from sources" although a generative model is configured

The generative answerer failed and the extractive fallback answered. The answer carries a
note naming the reason, and `adi_llm_fallbacks_total{answerer,reason}` counts it: a missing
API key in the environment, a rate limit, a timeout, or a malformed response.

## A document stays at "Processing"

Its worker died mid-run. After `STALLED_JOB_MINUTES` (120) it appears under
Administration → Processing → Stalled; `manage.py recover_stalled_jobs --apply --requeue`
recovers it.

## Reprocess answers 409

A run for the current version is already queued or processing. The response includes that
run; follow it rather than starting another — two runs over one version would race to be
the live extraction pass.

## Uploading as a system administrator fails with "not a registered department"

Departments are a registry. Create the department (Administration → Departments) before
uploading into it or assigning people to it.

## Sign-in fails with 403 "CSRF check failed"

The browser has no CSRF cookie, or the request came from another origin. The frontend
fetches `/api/auth/csrf/` first; behind a proxy, make sure the `Host` header is passed
through (Vite's proxy must not use `changeOrigin`) and that `CSRF_TRUSTED_ORIGINS` lists the
public `https://` origin.

## Permission errors writing media or backups in production

The application runs as uid 10001. Volumes created by an older root-running image, and a
host `./backups` directory, need a one-time `chown` — see
[deployment/production.md](../deployment/production.md#before-the-first-boot).

## Commands run from Git Bash on Windows find the wrong path

Git Bash rewrites arguments that look like POSIX paths (`/out/x.sh` becomes
`C:/Program Files/Git/out/x.sh`). Prefix `docker compose run …` with `MSYS_NO_PATHCONV=1`.

## The Django test runner crashes with "cannot pickle 'traceback' object"

That is `--parallel` hiding a real failure. Run the suite serially.

## The backend restarts forever after a reboot: `PermissionError: '/tmp/prometheus/...'`

**Cause.** The backend writes its Prometheus samples to a tmpfs. With Docker's default
options a tmpfs is world-writable only on a container's *first* start; on every restart
after that (a crash under `restart: unless-stopped`, a Docker daemon restart, a host
reboot) Docker remounts it root-owned 0755, and the unprivileged process cannot create its
files. Every Django process then dies at import. Seen on this project's production test
stack after the host rebooted.

**Now.** The compose file declares `uid=10001,gid=10001,mode=0700` on every such tmpfs, and
`infrastructure/check_deployment_config.py` refuses a tmpfs without them. A stack started
from an older compose file needs `docker compose … up -d` (which recreates the container)
once with the current one.

## A chat answer takes 10–20 seconds on a CPU-only server

**Cause.** The local cross-encoder. bge-reranker-base scores `RAG_RETRIEVAL_DEPTH` (20)
passages of up to 512 tokens per question; on a 4-core laptop CPU that measured 13.4 s of
a 13.8 s answer ([performance](../operations/performance.md)).

**Options.** `RAG_RERANKER_LOCAL_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2` reranks several
times faster and retrieved better on the evaluation benchmark, at the cost of answering
slightly fewer questions ([evaluation](../evaluation/README.md#calibration)); a GPU
(`RAG_RERANKER_LOCAL_DEVICE`); or a smaller `RAG_RETRIEVAL_DEPTH` (not measured).
