# API reference

JSON over HTTPS, same origin as the web application.

**Versioning (spec 16).** `/api/v1/` is the canonical prefix and what the web
application calls. Every path below is also served without the version (`/api/...`)
as a permanent alias for existing scripts and tokens; the tables use the short form.
When a `v2` ships, `v1` stays mounted until its clients have moved.

**Idempotency (spec 16).** `POST /api/v1/documents/` and
`POST /api/v1/documents/{id}/versions/` accept an `Idempotency-Key` header (1-255
printable ASCII characters). A retry with the same key within 24 hours returns the first
result (**202**, header `Idempotent-Replayed: true`) instead of creating a duplicate. The
same key with a different file or title is **422**; a retry while the first request is
still running is **409**. Keys are per user. A request that fails releases its key.
Without the header, behaviour is unchanged.

## Authentication

| Client | Mechanism |
|---|---|
| Browser | `GET /api/auth/csrf/` sets the CSRF cookie; `POST /api/auth/login/` with `X-CSRFToken` sets an **httpOnly session cookie** (8 h absolute). Every unsafe request carries `X-CSRFToken`. |
| Script / service | `POST /api/auth/token/` returns a token; send `Authorization: Token <key>`. Tokens **expire** after `TOKEN_TTL_HOURS` (12 by default); an expired token is deleted and a fresh one issued on the next `/token/` call. |

Unauthenticated requests get **401**. Bad credentials get **400** with one message whether
or not the user exists. Login endpoints are throttled per IP and username (10/min) and
rate-limited again at nginx.

## Errors

DRF shapes: `{"detail": "..."}` or field errors `{"field": ["..."]}`. **404** is returned
both for ids that do not exist and for documents or conversations the caller may not read —
the bodies are identical, so ids cannot be probed. **403** means the caller can see the
object but may not perform the action. **409** means a conflict with current state (a run
already in progress, an archived document). **429** is a rate limit.

## Identity

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/auth/csrf/` | anyone | Sets the CSRF cookie |
| POST | `/api/auth/login/` | anyone | `{username, password}`; CSRF enforced even though anonymous; audited |
| POST | `/api/auth/logout/` | signed in | Ends the session; with a token, deletes it |
| POST | `/api/auth/password/` | signed in | `{current_password, new_password}`; ends other sessions, revokes tokens |
| POST | `/api/auth/token/` | anyone | Script login; expiring token |
| GET | `/api/auth/me/` | signed in | Profile, role, department and `capabilities` |
| GET | `/api/departments/` | signed in | Active departments `{id, name, code}` |
| GET | `/api/dashboard/` | signed in | Counts and lists over the caller's visible documents |

## Documents

Read = owner, same department, or system admin. Manage = owner, department admin of that
department, or system admin. Every read and write below is written to the audit log, with
two exceptions: the listing (`GET /api/documents/`) is not audited, and the processing timeline
(`GET /api/documents/{id}/processing/`, which the document screen polls) is audited only when
the caller is refused.

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/api/documents/` | read | Paginated (50). `search`, `status`, `document_type`, `department`, `owner` (id or `me`), `created_after/before`, `updated_after/before`, `ordering` (≤3 of `id,title,status,document_type,created_at,updated_at`, `-` for descending), `archived` = `false` (default) / `true` / `all` |
| POST | `/api/documents/` | any signed-in user | multipart `title`, `document_type`, `file`, and `department` (system admins only; must be a registered department). **202** — processing is asynchronous |
| GET | `/api/documents/{id}/` | read | Includes `current_version`, `owner_username`, `is_archived`, `can_manage` |
| PATCH | `/api/documents/{id}/` | manage | multipart `title`, `document_type` |
| DELETE | `/api/documents/{id}/` | manage | Permanent: versions, content, index and files |
| POST | `/api/documents/{id}/archive/` · `/restore/` | manage | Archived documents leave search, answers and the default listing |
| GET · POST | `/api/documents/{id}/versions/` | read · manage | POST multipart `file` → new immutable version, **202**; **409** if archived |
| GET | `/api/documents/{id}/download/[?version=N]` | read | Original bytes; the version is recorded on the audit row. **403** for a version the scanner quarantined, except to a system admin |
| POST | `/api/documents/{id}/reprocess/` | manage | **202** with the job; **409** with the running job if one is queued or processing |
| GET | `/api/documents/{id}/processing/` | read | Every job with its stage events |
| GET | `/api/documents/{id}/content/` | read | Page texts of the live extraction pass |
| GET | `/api/documents/{id}/classification/` | read | Every verdict, with model and version |
| GET | `/api/documents/{id}/extraction/` | read | Every field-extraction pass; one row per declared field, including NOT_FOUND and CONFLICTED |
| GET | `/api/documents/{id}/tables/` · `/figures/` | read | Detected tables (cells + span) and located figures |
| GET | `/api/documents/{id}/chunks/` | read | Indexed chunks with page, span and embedding identity (paginated) |
| GET | `/api/documents/{id}/source/?chunk=C` | read | The page text of the pass chunk C came from, its span, and `is_current` |

## Search and chat

| Method | Path | Notes |
|---|---|---|
| POST | `/api/search/` | `{query, limit≤50, document_type?, department?, document_id?, collection_id?}`. POST so queries stay out of URLs and logs. Only authorized, current, non-archived content is searched |
| POST | `/api/chat/` | `{question, conversation_id?, document_type?, department?, document_id?, collection_id?}` → answer, citations, abstention reason, grounding, retrieval counts, model, plan, `timings`, notes, `resolved_question` for follow-ups |
| GET | `/api/chat/conversations/` | The caller's conversations, newest first |
| GET · PATCH · DELETE | `/api/chat/conversations/{id}/` | Owner only; PATCH `{title}` |
| POST | `/api/messages/{id}/feedback/` | `{rating: HELPFUL \| NOT_HELPFUL \| INCORRECT, comment?}`; owner of the conversation only |
| GET | `/api/evaluations/…` | Evaluation runs and per-case results, scoped by dataset department |

The question text is never written to the audit log; only that a search or question
happened, and counts.

## Collections

Named sets of documents (spec 14, 15.2). A collection **narrows, never grants**: search
and chat apply the caller's document permissions first and `collection_id` after, and a
collection the caller cannot see narrows to nothing. A collection belongs to one
department and holds only that department's documents.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/collections/` | signed in | Own department's collections, plus any the caller created; system admins see all. Each has `documents` (`{id, title}`, only those the caller may read), `document_count`, `can_manage` |
| POST | `/api/collections/` | system admin, department admin, faculty (`capabilities.create_collections`) | `{name, description?}`; `department` honoured for system admins only. **201**; duplicate name in the department **400**. Audited |
| GET · PATCH · DELETE | `/api/collections/{id}/` | read · manage | PATCH `{name?, description?}`. DELETE keeps the documents. Manage = creator, the department's admin, or a system admin. Invisible ids are **404** |
| POST · DELETE | `/api/collections/{id}/documents/` | manage | `{document_ids: [...]}` (≤500). Adding a document the caller cannot read, or from another department, is **404** naming the ids. Audited |

## Administration

System admins see everything. Department admins see every account in their own department,
but may manage only its FACULTY, STAFF and STUDENT accounts; they may not create or edit
administrators, and other admins and superusers appear in their list with `can_manage: false`. Nobody can
change their own role or deactivate themselves.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET · POST | `/api/admin/users/` | admins | Filters `search`, `role`, `department`, `is_active`. Each row carries `can_manage` - whether the caller may edit it. POST requires an initial password (validated); a department admin's new account goes into their department, which must be active |
| GET · PATCH | `/api/admin/users/{id}/` | admins, scoped | Username immutable. Deactivation deletes the account's sessions and tokens. Superusers can be edited only by superusers; staff accounts not by department admins |
| POST | `/api/admin/users/{id}/set-password/` | admins, scoped | Ends the user's sessions and tokens |
| GET | `/api/admin/users/roles/` | admins | Roles this admin may assign |
| GET · POST · PATCH | `/api/admin/departments/[{id}/]` | read: admins · write: system admins | Paginated. Case-insensitive unique names; rename rewrites every reference atomically, and is refused (400) when records already use the new spelling - that would merge two tenants; no DELETE (deactivate instead). `member_count` and `document_count` are null for departments other than a department admin's own |
| GET | `/api/admin/audit/` | admins, scoped | `action`, `result`, `user`, `document`, `since`, `until` (dates or full ISO datetimes). Department admins see rows by their members or on their documents; for another department's document both the id and the title are withheld (`document_withheld: true`), and filtering by such an id returns nothing |
| GET | `/api/admin/jobs/` | admins, scoped | `status`, `error_code`, `stalled=true` |
| POST | `/api/admin/jobs/{id}/retry/` | admins, scoped | **409** if a run is active; **400** for a superseded version |
| GET | `/api/admin/system/` | system admins | Dependency health, queue depths, counts, AI configuration — names only, never secrets |

## Operational

| Path | Notes |
|---|---|
| `/health/` | Liveness: process up, database reachable. Deliberately not dependent on Redis |
| `/ready/` | Readiness: database, cache, broker. 503 with per-component status, never hosts or error text |
| `/metrics` | Prometheus, scraped inside the compose network. nginx answers 404 for it, so it is never reachable through the edge; `METRICS_ALLOWED_IPS` optionally restricts it further |
| `/django-admin/` | Django admin (superusers). `/admin/*` is the application's own administration screens |
