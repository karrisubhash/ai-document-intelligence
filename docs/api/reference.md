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
| GET | `/api/auth/me/` | signed in | Profile, role, department and `capabilities` |
| GET | `/api/departments/` | signed in | Active departments `{id, name, code}` |
| GET | `/api/dashboard/` | signed in | Counts and lists over the caller's visible documents; `pending_signup_requests` - how many requests this administrator reviews - is null for non-administrators |

Signing in (session or token) with the exact username and the password of a request that
is still waiting, or was declined, is **403** `{detail, code}` with `code` `signup_pending`
or `signup_rejected` (the detail carries the reviewer's reason). Without the right password
it is the ordinary **400**, and it takes as long: every failed sign-in costs the same one
extra password hash, whoever holds the name.

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
| GET | `/api/documents/{id}/insights/` | read | The extractive brief — see [Document intelligence](#document-intelligence). Not audited when allowed |
| GET | `/api/documents/{id}/related/` | read | Up to 5 related documents. Throttled (`insights`). Not audited when allowed |
| GET | `/api/documents/{id}/thumbnail/` | read | First-page image or **404**. Not audited when allowed |
| GET | `/api/documents/suggestions/` | signed in | Questions across readable documents. Throttled (`insights`) |
| GET | `/api/documents/deadlines/?days=90` | signed in | Upcoming dates in readable documents. Throttled (`insights`) |

## Document intelligence

Computed after a document is processed, on CPU, with no language model: an extractive
brief, typed facts, related documents and a thumbnail (`docs/ai/pipeline.md`, "Document
intelligence"). Every cross-document read is scoped to the documents the caller can read
first; a document the caller cannot read is a **404** on its own endpoints and never appears
in another's results. Offsets (`start`, `end`) are Unicode code points into the stored page
text of the live extraction pass, and re-slice exactly; open them with
`/documents/{id}?tab=text&page=P&start=S&end=E`. The three cross-document reads, and a PATCH
that changes `document_type`, share the per-user `insights` throttle (`THROTTLE_INSIGHTS`,
600/hour).

**Additions to the document shapes** (additive; the list omits `counts` and `collections`):

| Field | List | Detail | Meaning |
|---|---|---|---|
| `document_type_source` | ✓ | ✓ | `default` (the upload default, GENERAL), `auto` (set by the type detector), `user` (chosen by a person; the detector never changes it). Read-only |
| `type_confidence` | ✓ | ✓ | The detector's confidence (0–1) when `auto`, else `null`. Read-only |
| `page_count` | ✓ | ✓ | Pages of the live extraction pass, or `null` |
| `has_thumbnail` | ✓ | ✓ | Whether `/thumbnail/` answers with an image |
| `current_stage` | ✓ | ✓ | While QUEUED/PROCESSING: the running job's stage (e.g. `"OCR"`) or `null` before it starts; `null` otherwise |
| `counts` | | ✓ | `{"tables": int, "figures": int, "fields_found": int}` for the live pass; `fields_found` is the newest extraction under the current type |
| `collections` | | ✓ | `[{"id": int, "name": str}]` — only collections the caller can see |
| `current_version.uploaded_by_name` | | ✓ | The uploader's full name or username (also on `/versions/`) |

`current_version.file` (the storage key) is no longer returned anywhere. A **PATCH** that
*changes* `document_type` sets `document_type_source` to `user`, clears `type_confidence`
and queues re-extraction under the new type plus a new brief (not a reprocess); sending the
unchanged type with a title edit changes nothing. **POST `/api/documents/`** (and its
idempotent replay) adds `possible_duplicates`: `[{"id", "title", "status", "is_archived",
"is_current_version"}]` — other documents the uploader can read that hold a file with the
same SHA-256 (at most 5). It never blocks the upload. **GET `.../classification/`** rows add
`taxonomy` (`document_type` | `layout_class`) and `abstained` (bool); document-type verdicts
are listed first, and the model artifact's server path is no longer in `metadata`.

**`GET /api/v1/documents/{id}/insights/`**

```json
{ "document_id": 7, "version_id": 3, "content_id": 9,
  "status": "READY",                    // READY | PENDING | FAILED | UNAVAILABLE
  "method": "extractive-v1", "generated_at": "2026-11-07T10:01:00Z",   // null unless READY
  "summary":    [{"text": "...", "page_number": 1, "start": 120, "end": 160}],
  "key_points": [{"text": "...", "page_number": 1, "start": 200, "end": 229,
                  "kind": "deadline"}],  // deadline|obligation|figure|date|clause|heading
  "key_phrases": [{"text": "bus pass", "score": 1.0}],                 // best first, score 0-1
  "entities": [{"type": "DEADLINE", "text": "15th March 2027", "normalized": "2027-03-15",
                "page_number": 2, "start": 10, "end": 25, "count": 1,
                "label": "Last date for renewal of the bus pass"}],
  "stats": {"pages": 2, "words": 1200, "reading_minutes": 6, "ocr_pages": 0, "language": "en"},
  "suggested_questions": ["What is the last date for renewal of the bus pass?"],
  "document_type": {"value": "CIRCULAR", "source": "auto", "confidence": 0.97},
  "ai": null }
```

`status`: `PENDING` while the document is processing or its brief is queued; `FAILED` when
the generator failed (the document itself is unaffected); `UNAVAILABLE` when there is no
live pass (quarantined, failed processing) or enrichment is switched off. Unless `READY`,
the lists are empty and `stats` is zeros with `language: "unknown"`. Entity `type` is one of
`DATE DEADLINE MONEY PERCENT DURATION EMAIL PHONE URL REFERENCE COURSE_CODE ORG`;
`normalized` is an ISO date (or `YYYY-MM` for a month), `"INR 4500"` / `"USD 300"`, `"75%"`,
an ISO-8601 duration (`P15D`, `PT2H`), a lower-cased email, an E.164 phone (`+914023456789`),
a URL with lower-cased scheme and host, an upper-cased reference or course code, a
whitespace-collapsed organisation name — or `null`. `label` (additive to the original
contract) says what a date is for, or `""`. `stats.language` is `en | hi | te | mixed |
unknown`. `ai` is reserved for an on-demand AI brief: `{"summary", "questions", "model",
"generated_at"}`, `null` until one exists (none is generated yet).

**`GET /api/v1/documents/{id}/related/`** → `{"results": [{"id", "title", "document_type",
"department", "updated_at", "similarity": 0.0-1.0, "near_duplicate": bool}], "method":
"semantic" | "lexical" | "unavailable"}`. At most 5, readable, live (processed, not
quarantined) and not archived. `semantic`: cosine between document centroids (mean chunk
vectors of a semantic embedding model), below `DOCUMENT_RELATED_MIN_SIMILARITY` (0.5) not
shown. `lexical` (no semantic vectors): harmonic mean of key-phrase overlap both ways, below
0.2 not shown. `near_duplicate`: similarity ≥ `DOCUMENT_NEAR_DUPLICATE_SIMILARITY` (0.97), or
the same file (SHA-256) or the same text.

**`GET /api/v1/documents/{id}/thumbnail/`** → the image (`image/webp`, or `image/png`), ~320
px wide, of the current version's first page, with `Cache-Control: private, max-age=300`,
`ETag` and `Last-Modified`; `If-None-Match` answers **304**. **404** when there is none (Word
and text files, not yet rendered) or the version is quarantined. Append `?v=<version id>` to
key the browser cache to the version.

**`GET /api/v1/documents/suggestions/`** → `{"results": [{"question", "document_id",
"document_title"}]}` — at most 8, one per document first, from the most recently updated
readable documents with a brief. The selection is cached per user for
`DOCUMENT_SUGGESTIONS_CACHE_SECONDS` (600); what the user may read is checked again on
every request.

**`GET /api/v1/documents/deadlines/?days=90`** → `{"results": [{"date": "YYYY-MM-DD", "type":
"DEADLINE" | "DATE", "label", "text", "document_id", "document_title", "page_number",
"start", "end"}]}` — at most 20, soonest first (deadlines before plain dates on the same
day, one row per document and date), from today to today + `days` (1–366, default 90;
anything else is **400** `{"days": [...]}`), from the live pass of readable, non-archived
documents. `type` is additive to the original contract.

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
