# Document Intelligence Console

A React + Vite frontend for **exercising the API** — uploading documents, watching them
move through the pipeline stage by stage, and reading what each AI stage actually produced.

It is a testing console, not the product frontend the specification calls for (§26). The
distinction matters in one direction: this app is built to show what the backend did,
including when the backend deliberately did nothing. A stage that recorded `SKIPPED`, a
field that came back `NOT_FOUND`, an answer that abstained, an embedding that is lexical
rather than semantic — all of them are rendered as the results they are, never smoothed
into a blank panel or a plausible-looking value.

---

## Running it

The API must be up first (`docker compose up` from the repository root).

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Sign in with any Django user. If the instance has none:

```bash
docker compose exec backend python manage.py createsuperuser
```

### Against another API

```bash
cp .env.example .env          # then set VITE_API_BASE_URL
```

The target instance has to allow this origin — `CORS_ALLOWED_ORIGINS` in its environment.
A local API running with `DJANGO_DEBUG=True` already allows `http://localhost:5173`.

### Under Docker instead

```bash
docker compose --profile frontend up
```

Off by default: `npm run dev` on the host is the faster loop, and this service exists for
machines without Node. The port is pinned to 5173 on both sides because the API's
development CORS default names that exact origin.

---

## What each screen tests

| Screen | Endpoints | What it makes visible |
|---|---|---|
| **Documents** | `POST/GET /api/documents/` | Upload returns **202**, not a finished document. The list follows anything still `QUEUED`/`PROCESSING` and stops polling when the queue is idle. |
| **Pipeline** tab | `/processing/` | Every stage of every job: status, message, duration, and the structured metadata each stage recorded. Failed attempts stay on record beside the successful one. |
| **Content** tab | `/content/` | The normalized per-page text, with the extraction method that produced each page — the PDF text layer and OCR are separately labelled, because they are separately measurable. |
| **Classification** tab | `/classification/` | Every verdict with the model and version that produced it. |
| **Fields** tab | `/extraction/` | One row per declared field, including `NOT_FOUND`, `INVALID` and `CONFLICTED`, each with its page and character span. |
| **Chunks** tab | `/chunks/` | Retrievable units with page and offsets, and whether an embedding exists and whether it is semantic. |
| **Versions** tab | `/versions/` | Immutable versions with size, MIME type and SHA-256. |
| **Search** | `POST /api/search/` | Hybrid results with the fusion score, which half matched, each half's rank, and the candidate counts behind them. |
| **Ask** | `POST /api/chat/` | The answer, its citations, the grounding support ratio, retrieval and reranking counts, suspected injections, and whether the answerer was generative at all. |

Reprocess, download, delete, metadata edits and new-version uploads are on the document
detail screen.

### Reading the honest bits

Several panels exist to stop a demo from overstating the system:

- **"lexical vectors", not "semantic"** on search results — the bundled embedding provider
  hashes n-grams, so it scores vocabulary overlap. A miss is not a failure to understand.
- **"extractive", not "generative"** on answers — the bundled answerer quotes retrieved
  passages verbatim. It cannot hallucinate, and it cannot synthesise either.
- **`SKIPPED` stages** — classification and extraction are skipped, not guessed, while
  `DOCUMENT_CLASSIFIER` / `DOCUMENT_FIELD_EXTRACTOR` are unset. The Pipeline tab always
  says which of "not configured" and "not reached" applies.
- **Abstention** — an abstained answer is a successful outcome, and is shown as one.

To see the configured paths instead of the skipped ones, set `DOCUMENT_CLASSIFIER`,
`DOCUMENT_FIELD_EXTRACTOR` and `DOCUMENT_EMBEDDING_PROVIDER` in the API's environment
(see the root README) and reprocess a document from the detail screen.

---

## Layout

```
src/
  api/
    types.ts        response shapes, mirroring the DRF serializers
    client.ts       fetch wrapper, token handling, typed endpoint calls
    hooks.ts        useResource (load + reload) and usePolling (follow the queue)
  auth/
    AuthContext.tsx token acquisition, verification and sign-out
  components/
    ui.tsx          pills, banners, tabs, spans, JSON disclosure, formatters
    tabs/           one component per document-detail tab
  pages/            login, documents, document detail, search, chat
  styles.css        one palette, defined light-first with a dark override
```

Notes on two choices that are easy to misread:

- **The token is kept in `localStorage`.** A development-tool tradeoff, not a
  recommendation — any script on the origin can read it. The alternative, an httpOnly
  cookie, needs the session/CSRF path that the API's CORS configuration deliberately does
  not open (`CORS_ALLOW_CREDENTIALS` stays off).
- **Errors are raised, never defaulted away.** A console whose job is to report what the
  backend did is the last place that should turn a failed call into an empty list.

## Checks

```bash
npm run typecheck    # tsc --noEmit
npm run build        # typecheck + production bundle
```
