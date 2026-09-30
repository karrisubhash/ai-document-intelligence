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
answer's *How this answer was produced* panel shows the resolved query; check which terms
were borrowed. `RAG_FOLLOWUP_HISTORY_TURNS=0` disables resolution entirely.

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
