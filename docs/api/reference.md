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
same key with a different file (compared by content) or title is **422**; a retry while
the first request is still running is **409**. Keys are per user. A request that fails
releases its key; one whose server process died mid-request holds it for at most 10
minutes. Without the header, behaviour is unchanged.

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
| GET | `/api/auth/signup/` | anyone | `{enabled, departments, roles, email_domains}` for the sign-up form: active departments `{id, name, code}`, the roles an applicant may ask for, and the `SIGNUP_EMAIL_DOMAINS` allowlist (empty: any address). With `SIGNUP_ENABLED=False`, `enabled: false` and empty lists. Unthrottled |
| POST | `/api/auth/signup/` | anyone | `{username, email, first_name, last_name?, password, department?, role?, note?}` files a request - it creates no account. **201** `{status: "PENDING"}`. CSRF enforced; password validated and stored only as a hash; the username is NFKC-normalised, must be ASCII, and is refused (**400**, one message either way) when an account or a waiting request holds it, case-insensitively; the email's domain is lower-cased and must match the allowlist if one is set (subdomains count); `role` is FACULTY, STAFF or STUDENT. **403** when turned off; **429** with `SIGNUP_MAX_PENDING` requests waiting (`code` `signup_queue_full`), or past `THROTTLE_SIGNUP` (100/hour per address - counting attempts that pass CSRF, the switch and the queue check, failed ones included). Audited (`SIGNUP_REQUEST`) |
| GET | `/api/auth/demo/` | anyone | `{enabled}`: whether the one-click demo is offered - `DEMO_ENABLED` on, its `DEMO_DEPARTMENT` active and holding at least one Ready document. Unthrottled (read on every landing-page visit) |
| POST | `/api/auth/demo/` | anyone | No body. Makes a fresh demo guest (see below), signs it in with a session and answers **201** with exactly the body of `POST /api/auth/login/`. CSRF enforced. **404** when the demo is not offered; **429** past `THROTTLE_DEMO` (10/hour per address) or `THROTTLE_DEMO_GLOBAL` (60/hour, all addresses), or with `DEMO_MAX_GUESTS` guests existing (`code` `demo_full`). Refused requests (CSRF, demo off) spend no budget. Audited (`LOGIN`, `method: "demo"`); sweeps up to 20 expired guests first (`GUEST_PURGE`) |
| GET | `/api/auth/me/` | signed in | Profile, role, department and `capabilities`, which include `is_guest` (true for a demo guest) |
| GET | `/api/departments/` | signed in | Active departments `{id, name, code}` |
| GET | `/api/dashboard/` | signed in | Counts and lists over the caller's visible documents; `pending_signup_requests` - how many requests this administrator reviews - is null for non-administrators |

Signing in (session or token) with the exact username and the password of a request that
is still waiting, or was declined, is **403** `{detail, code}` with `code` `signup_pending`
or `signup_rejected` (the detail carries the reviewer's reason). Without the right password
it is the ordinary **400**, and it takes as long: every failed sign-in costs the same one
extra password hash, whoever holds the name.

**Demo guests** (`capabilities.is_guest`). An account made by `POST /api/auth/demo/`: named
`guest-` and eight random characters, no usable password, STUDENT in `DEMO_DEPARTMENT`,
deleted after `DEMO_GUEST_TTL_HOURS`. Every request a guest makes other than GET, HEAD and
OPTIONS is **403** `{detail, code: "demo_read_only"}` - before the view runs - except:
`POST /api/chat/`, `POST /api/chat/stream/`, `POST /api/search/`,
`POST /api/messages/{id}/feedback/`, `POST /api/notifications/{id}/read/`,
`POST /api/notifications/read-all/`, `POST /api/auth/logout/`, and the public doors
(`auth/login/`, `auth/demo/`, `auth/signup/`, `auth/token/`), which never act on the
signed-in account. Chat and search also spend guest budgets on top of their own throttles:
per guest `THROTTLE_GUEST_CHAT` (20/hour), `THROTTLE_GUEST_CHAT_BURST` (5/minute),
`THROTTLE_GUEST_SEARCH` (60/hour); for all guests together `THROTTLE_GUEST_CHAT_GLOBAL`
(120/hour) and `THROTTLE_GUEST_SEARCH_GLOBAL` (600/hour). Past one: **429**
`{detail, code: "demo_throttled"}` with `Retry-After`. A guest's API token is refused
(**401**). Usernames beginning `guest-` are refused by sign-up and account creation
(**400** on `username`).

## Documents

Read = owner, same department, or system admin. Manage = owner, department admin of that
department, or system admin. Every read and write below is written to the audit log, with
two exceptions: the listing (`GET /api/documents/`) is not audited, and the processing timeline
(`GET /api/documents/{id}/processing/`, which the document screen polls) is audited only when
the caller is refused.

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/api/documents/` | read | Paginated (50); each row carries `can_manage`. `search`, `status`, `document_type`, `department`, `owner` (id or `me`), `created_after/before`, `updated_after/before`, `ordering` (≤3 of `id,title,status,document_type,created_at,updated_at`, `-` for descending), `archived` = `false` (default) / `true` / `all` |
| POST | `/api/documents/` | any signed-in user | multipart `title`, `document_type`, `file`, and `department` (system admins only; must be a registered department). **202** — processing is asynchronous |
| GET | `/api/documents/{id}/` | read | Includes `current_version`, `owner_username`, `is_archived`, `can_manage` |
| PATCH | `/api/documents/{id}/` | manage | multipart `title`, `document_type` |
| DELETE | `/api/documents/{id}/` | manage | Permanent: versions, content, index and files |
| POST | `/api/documents/bulk-delete/` | manage (each) | JSON, exactly one of `{"ids": [...]}` (≤100) or `{"all": true, "filters": {<listing parameters>}, "as_of"?, "preview"?}`. Unknown keys, inside `filters` too, are **400** - never ignored. **200** `{deleted, forbidden, not_found, failed}`, each a list of ids; a document the caller cannot see is `not_found`; `failed` means its delete itself failed (e.g. still being processed) and the rest of the batch went on. `all` deletes at most 100 per request, only documents created by `as_of`, and adds `remaining` (repeat, sending back `as_of`, until 0), `skipped` (matching and visible, but not the caller's to delete) and `as_of` (the snapshot; now, if none was sent). `preview: true` deletes nothing and answers those counts. Throttled (`THROTTLE_BULK_DELETE`, 300/hour). Audited per document as `DOCUMENT_DELETE` with `bulk: true` |
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
| POST | `/api/chat/` | `{question, conversation_id?, document_type?, department?, document_id?, collection_id?}` → answer, citations, abstention reason, grounding, retrieval counts, model, plan, `timings`, notes, `resolved_question` for follow-ups, and `source`: `documents` (grounded, cited), `general` (general knowledge, uncited; only with `CHAT_GENERAL_KNOWLEDGE`) or `none` (abstained) |
| POST | `/api/chat/stream/` | Same request and the same turn, as Server-Sent Events: `delta {text}` while the model writes (provisional), `reset {}` (discard what was shown), then `done {…the /chat/ payload}` (authoritative) or `error {detail}`. Validation, throttling and conversation ownership fail as ordinary JSON 4xx before streaming starts. Extractive answers send only `done` |
| GET | `/api/chat/conversations/` | The caller's conversations, newest first |
| GET · PATCH · DELETE | `/api/chat/conversations/{id}/` | Owner only; PATCH `{title}` |
| POST | `/api/chat/conversations/bulk-delete/` | Owner only. JSON `{"ids": [...]}` (≤100) or `{"all": true, "as_of"?}` (the caller's conversations, ≤100 per request, with `remaining` and `as_of` as for documents). Unknown keys are **400**. **200** `{deleted, not_found}`; another user's id is `not_found` |
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
| POST | `/api/collections/` | system admin, department admin, faculty (`capabilities.create_collections`) | `{name, description?}`; `department` honoured for system admins only, defaulting to their own (**400** if they have none). **201**; duplicate name in the department **400**. Audited |
| GET · PATCH · DELETE | `/api/collections/{id}/` | read · manage | PATCH `{name?, description?}` (any other body is **400**). DELETE keeps the documents. Manage = creator, the department's admin, or a system admin. Invisible ids are **404** |
| POST | `/api/collections/bulk-delete/` | manage (each) | JSON `{"ids": [...]}` (≤100) or `{"all": true, "as_of"?}` (the collections the caller may manage, ≤100 per request, with `remaining` and `as_of` as for documents). Unknown keys are **400**. **200** `{deleted, forbidden, not_found}`; an invisible collection, or one deleted meanwhile, is `not_found`. `all` adds `skipped` (visible, not the caller's to manage). Documents are kept. Audited per collection with `bulk: true` |
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
| POST | `/api/admin/samples/` | system admins | `{department}` - a registered, active department, matched case-insensitively. Loads the sample library into it through the upload path: **202** `{queued, skipped}` - samples created and queued for processing, and samples the department already held (any version of any document there with the same checksum, archived ones included). Owned by the inactive `sample-library` account. **400** on `department`. Throttled as `samples` (`THROTTLE_SAMPLES`, 10/hour). Audited: one `SAMPLES_LOAD` (`department`, `queued`, `skipped`, `via: "api"`) and a `DOCUMENT_CREATE` per sample, naming the administrator |

Demo guests are not listed by, and cannot be edited or given a password through, the user
endpoints (**404**), and are not counted in a department's `member_count`.
| GET | `/api/admin/signup-requests/[{id}/]` | admins, scoped | Filters `status` (`PENDING`, `APPROVED`, `REJECTED`) and `search`. Department admins see requests naming their department; one that names none is for system admins. Each waiting row carries `email_in_use`: usernames of accounts the caller administers that already have the address (nothing verifies an applicant's email). Never the password hash |
| POST | `/api/admin/signup-requests/{id}/approve/` | admins, scoped | `{role?, department?}`, defaulting to what was asked for and held to the rules for creating an account by hand (a department admin works in their own, active department) - and never an administrative role, from anyone: promote the account afterwards if needed. Creates the account with the password the applicant chose. **409** if already decided (checked first), or if an account has taken the username since. Audited with the account in one transaction (`SIGNUP_APPROVE`, with the applicant's username and the new account's id) |
| POST | `/api/admin/signup-requests/{id}/reject/` | admins, scoped | `{note?}` - shown to the applicant when they next try to sign in. **409** if already decided. The applicant may apply again. Audited (`SIGNUP_REJECT`, with the username) |
| POST | `/api/admin/signup-requests/bulk-reject/` | admins, scoped | Decline `{ids: [...]}` (≤100) or `{all: true, search?, as_of?, preview?}` - every waiting request the caller decides that matches `search` - with an optional `note` for each. Like the bulk deletes: at most 100 per call, answering `rejected`, `not_found` (not the caller's, or decided meanwhile - alike) and, with `all`, `remaining` and the `as_of` snapshot to repeat with; `preview: true` only counts. Each decline audited as a single one, with `bulk: true`. Throttled as `bulk_delete` |
| GET · POST · PATCH | `/api/admin/departments/[{id}/]` | read: admins · write: system admins | Paginated. Case-insensitive unique names; rename rewrites every reference atomically, and is refused (400) when records already use the new spelling - that would merge two tenants; no DELETE (deactivate instead). `member_count` and `document_count` are null for departments other than a department admin's own |
| GET | `/api/admin/audit/` | admins, scoped | `action`, `result`, `user`, `document`, `since`, `until` (dates or full ISO datetimes). Department admins see rows by their members, on their documents, and about their department's collections and sign-up requests (whoever acted); for another department's document both the id and the title are withheld (`document_withheld: true`), and filtering by such an id returns nothing |
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
