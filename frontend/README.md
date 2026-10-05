# Document Intelligence — web application

The product frontend: sign in, upload and browse documents, read what was extracted,
search, ask questions with cited answers, and — for administrators — manage people,
departments and processing.

React 19 + TypeScript + Vite, no UI framework. It talks to the Django API on the **same
origin**: Vite proxies `/api` in development and nginx does the same in production, so
the browser holds an httpOnly session cookie that no script can read, and every unsafe
request carries Django's CSRF token. Nothing credential-like is stored in `localStorage`.

---

## Running it

The API must be up first (`docker compose up` from the repository root).

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxied to http://localhost:8000
```

Point the proxy elsewhere with `VITE_PROXY_TARGET` (for example the e2e backend on
`http://localhost:8001`). Accounts come from the Django admin (`/django-admin/`; `/admin/*` is this app's own administration) or the Users screen;
`python manage.py createsuperuser` makes the first one.

## Checks

```bash
npm run typecheck    # tsc --noEmit
npm test             # Vitest + Testing Library, jsdom
npm run build        # typecheck + production bundle in dist/
```

### End-to-end (Playwright, against a live stack)

`e2e/platform.spec.ts` drives a real browser through the whole product path — upload,
processing, search, a cited answer, a follow-up, an abstention, the department boundary,
administration and the audit trail — against real Django, Celery, PostgreSQL, Redis and
models. It uses the Chrome or Edge already installed (`E2E_CHANNEL=chrome|msedge`), so no
browser download is needed. See [docs/development/testing.md](../docs/development/testing.md)
for preparing the disposable database and accounts it expects.

```bash
E2E_PASSWORD=... npx playwright test
```

---

## Screens

| Screen | What it does |
|---|---|
| **Sign in** | Session login with CSRF; explains an expired session, a throttled attempt or an unreachable server differently. |
| **Dashboard** | Counts over the documents the user may open, recent documents, failures they can act on, recent conversations, and a question box. |
| **Documents** | Search titles, filter by status/type/department/owner, archived toggle, sort, paginate; state lives in the URL. Polls only while something is processing. |
| **Document** | Overview and classification; Preview (PDF and images, never for quarantined files); Text by page with OCR flagged; Fields with their source spans; Tables; Figures; Versions with per-version download; Processing timeline. Edit, new version, reprocess, archive/restore and delete for those who may manage it. |
| **Search** | Hybrid search with the query kept out of the URL (the API takes it by POST for the same reason). Results highlight matched terms and open the document at the exact passage. |
| **Ask** | Conversations with history, rename and delete; answers with numbered citations that open the exact evidence — the cited passage tinted, the quoted words marked, and a warning when the document has changed since. Abstentions are explained in plain language; follow-ups show how they were read; injection warnings and multi-part plans are surfaced. Feedback per answer. |
| **Account** | Profile and password change. |
| **Administration** | Users (scoped for department admins), Departments (registry with cascading rename), Processing (failed/running/stalled runs with retry), Audit log, System status. Each screen appears only for the capabilities `/api/auth/me/` reports — and the API enforces the same rules regardless. |

## Layout

```
src/
  api/           client (same-origin fetch, CSRF, upload progress), types, hooks
  auth/          session context and capability checks
  components/    layout, UI primitives, upload dialog, page text viewer,
                 document panels, chat answer + source inspector
  lib/           formatting, labels, highlighting, safe Markdown, upload checks
  pages/         one file per screen; admin/ for administration
e2e/             Playwright specs and fixtures (need a running stack)
```

Two choices that are easy to misread:

- **Answers are rendered by a tiny Markdown renderer that never produces HTML from a
  string.** Answer text derives from uploaded documents, which are untrusted; React
  escapes every character, and there is no link or image rendering to abuse.
- **The UI hides what a user cannot do, but never decides it.** Every button maps to an
  endpoint that re-checks permission; a hand-typed admin URL gets an explanation, and a
  hand-crafted request gets a 403 or 404 from the API.
