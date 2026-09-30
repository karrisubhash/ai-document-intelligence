# Local development

## Prerequisites

- Docker with Compose v2 (the backend, worker, PostgreSQL and Redis run in containers)
- Node 22 for the frontend dev server
- Python 3.12+ only if you want to run `ruff` on the host (`backend/venv`)

The backend image carries Tesseract and poppler for OCR, and — when built with the optional
layers — scikit-learn, PyTorch (CPU) and sentence-transformers.

## First run

```bash
cp .env.example .env              # then set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build         # db, redis, backend (runserver on :8000), worker
docker compose exec backend python manage.py createsuperuser

cd frontend
npm install
npm run dev                       # http://localhost:5173, proxied to :8000
```

Sign in at <http://localhost:5173> with the superuser. It has the system administrator
role, so the Administration screens appear; create departments first, then accounts.

## Optional AI layers

| Build argument | Adds | Enables |
|---|---|---|
| `INSTALL_ML=true` | scikit-learn, joblib | the trained text classifier (`DOCUMENT_CLASSIFIER`) |
| `INSTALL_LOCAL=true` | CPU PyTorch, sentence-transformers | local semantic embeddings and cross-encoder reranking |
| `INSTALL_LLM=true` | anthropic, boto3, openai | Claude, Bedrock and OpenAI-compatible providers |

```bash
INSTALL_LOCAL=true INSTALL_ML=true docker compose up --build
```

then in `.env`:

```bash
DOCUMENT_EMBEDDING_PROVIDER=documents.processing.embedding.local.LocalEmbeddingProvider
RAG_RERANKER=documents.rag.local_rerank.LocalCrossEncoderReranker
```

Changing the embedding provider invalidates every stored vector; run
`docker compose exec backend python manage.py reindex` afterwards. Download the model
weights once with `python manage.py warm_models` — after that they load from the
`hf_cache` volume without contacting the Hugging Face Hub.

Other pipeline capabilities are off until named, and record `SKIPPED` rather than guess:

```bash
DOCUMENT_SCANNER=documents.processing.scanning.heuristic.HeuristicScanner
DOCUMENT_FIELD_EXTRACTOR=documents.processing.field_extraction.rule_extractor.RuleFieldExtractor
DOCUMENT_CLASSIFIER=documents.processing.classification.text_classifier.TextDocumentClassifier
```

## Everyday commands

```bash
docker compose run --rm -T -e LOG_LEVEL=ERROR backend python manage.py test --noinput
backend/venv/Scripts/ruff.exe check backend ai          # lint (ruff 0.16.8, pinned)
backend/venv/Scripts/ruff.exe format backend ai
cd frontend && npm test && npm run typecheck
```

Do not pass `--parallel` to the Django runner: one test error is not picklable and the
parallel runner then hides the real failure behind a pickling traceback.

## Keeping the development database safe

The `postgres_data` volume is the development database. Use a separately named database
for anything destructive — the e2e suite, benchmarks, restore drills:

```bash
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" adi_e2e'
docker compose run --rm -e POSTGRES_DB=adi_e2e backend python manage.py migrate
```

`seed_e2e` refuses to run against a database whose name does not contain `e2e`, `test` or
`ci`.
