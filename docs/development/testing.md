# Testing

Four layers, each catching what the others cannot.

| Layer | Where | Runs against | Command |
|---|---|---|---|
| Backend unit + integration | `backend/**/tests*.py` | real PostgreSQL + pgvector, Celery eager | `docker compose run --rm -T -e LOG_LEVEL=ERROR backend python manage.py test --noinput` |
| AI workspace | `ai/classification/tests/` | no Django or database, but scikit-learn, joblib and PyYAML (the workspace's dependencies, `ai/classification/pyproject.toml`) | `PYTHONPATH=. python -m unittest discover -s ai/classification/tests -t .` |
| Frontend unit + component | `frontend/src/**/*.test.ts(x)` | jsdom, mocked API | `cd frontend && npm test` |
| Browser end-to-end | `frontend/e2e/*.spec.ts` | a **live** stack: Django, Celery, PostgreSQL, Redis, models | see below |

## What the backend suite pins

The suite is organised by capability (`tests_security.py`, `tests_lifecycle.py`,
`tests_rag.py`, …). Worth knowing when changing code:

- **Authorization** — cross-department reads are 404 and indistinguishable from an unused
  id; readers who may not manage get 403; both are audited as DENIED
  (`tests_lifecycle.NoExistenceLeakTests`, `tests.py`).
- **AI settings are pinned to offline baselines under the test runner** regardless of
  `.env` (hashing embeddings, lexical reranker, extractive answerer). Tests that need a
  provider override it explicitly. Hosted providers are exercised with stub clients in
  `tests_providers.py`.
- **Concurrency** — `tests_lifecycle.ConcurrentVersionUploadTests` uses real threads and
  real transactions (`TransactionTestCase`).
- **Regression tests are written to fail without their fix**, and several say so in their
  docstring with the measured failure they replaced.

## The browser suite

`frontend/e2e/platform.spec.ts` walks the whole product in one serial run: a student
uploads a text file and a PDF and watches them process; searches and opens a hit at its
passage; asks a question and opens the citation's evidence; asks a follow-up; asks
something the documents cannot answer; a faculty member of another department is shown
nothing; a department admin creates an account and finds the other department's denied
attempt in the audit log; a system admin checks every dependency.

It needs a disposable database and the accounts `seed_e2e` creates:

```bash
# 1. a disposable database
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" adi_e2e'
docker compose run --rm -e POSTGRES_DB=adi_e2e backend python manage.py migrate

# 2. test accounts (refuses any database not named like a test one)
export E2E_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(16))')"
docker compose run --rm -e POSTGRES_DB=adi_e2e backend \
    python manage.py seed_e2e --password "$E2E_PASSWORD"

# 3. backend and worker on that database (here on :8001)
docker compose run -d --name adi-e2e-backend -p 8001:8000 -e POSTGRES_DB=adi_e2e \
    backend python manage.py runserver 0.0.0.0:8000
docker compose run -d --name adi-e2e-worker -e POSTGRES_DB=adi_e2e \
    worker celery -A config worker --queues=ingest,index --concurrency=2

# 4. the frontend, proxied to it, and the browser
cd frontend
VITE_PROXY_TARGET=http://localhost:8001 npx vite &
E2E_PASSWORD="$E2E_PASSWORD" npx playwright test
```

By default Playwright drives the installed Chrome (`E2E_CHANNEL=msedge` for Edge,
`chromium` for Playwright's bundled browser, which CI installs). Screenshots of each step
are written to `frontend/e2e/.results/screens/`.

**Last local runs:** 7/7 against the dev stack and 7/7 against the production stack over TLS, with the full configuration — local bge-small
embeddings, bge-reranker-base cross-encoder, the trained text classifier, rule-based field
extraction and the heuristic scanner. Two defects were found by it and fixed with
regression tests: a follow-up answered with the previous question's clause, and a
30-second first search caused by model-hub network calls (see
[troubleshooting](../troubleshooting/common-issues.md)).

The uploads land in whatever media volume the containers mount; remove them afterwards by
deleting the e2e documents through the e2e database (which removes exactly their files),
then drop the database.
