# Security model

## Identity

- **Browser sessions** are httpOnly, `Secure` (outside DEBUG), `SameSite=Lax` cookies,
  cached in Redis with a database copy, with an **8-hour absolute lifetime**
  (`SESSION_COOKIE_AGE`). Login rotates the session key (no fixation) and **enforces CSRF
  even though the caller is anonymous** — DRF would otherwise exempt it, enabling forged
  logins into an attacker's account. No credential is readable by JavaScript.
- **API tokens** (scripts, services) expire after `TOKEN_TTL_HOURS` and are deleted on
  first use after expiry. Logout with a token deletes it.
- **Passwords** use Django's validators (length, similarity, common, numeric). Changing a
  password — by the user or an admin — invalidates every other session (the session hash
  changes) and deletes every API token.
- **Deactivation** stops sign-in, rejects existing sessions and deletes tokens. Accounts are
  never deleted through the API, so their audit history keeps its subject.
- **Brute force** is limited per IP *and* username at the application (10/min on token,
  login and password endpoints) and at nginx (1 r/s burst 5 on the same paths).

**Not implemented:** SSO/SAML/OIDC, MFA, account lockout beyond throttling, password
expiry. These are institutional decisions with integration work attached.

## Authorization

The **department is the tenant boundary**, validated against a registry
(`accounts.Department`) so a typo can no longer create a tenant nobody belongs to.

| Role | Documents | Administration |
|---|---|---|
| SYSTEM_ADMIN | read and manage everything | everything |
| DEPARTMENT_ADMIN | read their department; manage it | their department's FACULTY/STAFF/STUDENT accounts; audit and jobs for their department |
| FACULTY, STAFF, STUDENT | read their department's and their own; manage their own | none |

An unset department is not a tenant: it matches nothing, so a new account sees only its own
uploads.

Enforcement is server-side, in plain functions (`documents/authorization.py`,
`accounts/permissions.py`) that every path calls — list, detail, every sub-resource,
search, chat, citations, dashboard, audit, jobs, evaluations. The frontend hides what a
user cannot do but decides nothing.

- **No existence leaks.** A document the caller may not read returns a 404 byte-identical
  to an unused id's, on every action including writes. Conversations are owner-only, also
  404. Department admins see audit rows about their members' actions elsewhere with the
  foreign document's **title withheld**.
- **Search and RAG filter before similarity.** Retrieval only ever ranks chunks from the
  authorized, non-archived, current version's live extraction pass; scoping a question to
  a document id the caller cannot read returns nothing rather than an error.
- **Privilege escalation** is blocked structurally: department admins cannot assign
  administrative roles, place anyone outside their department, or edit other admins;
  nobody can change their own role or deactivate themselves.

## Uploads and files

- Size, emptiness and extension are checked **before anything is stored**; oversize is a
  413. nginx caps bodies at 55 MB so the application produces the error.
- Ingestion sniffs magic bytes, so a mislabelled file never reaches the wrong parser.
- The **heuristic scanner** (enable with `DOCUMENT_SCANNER`) runs before validation and
  quarantines disguised executables, Office macros, PDF active content (`/JavaScript`,
  `/OpenAction`, `/Launch`, embedded files) and the EICAR string. **It is not an
  antivirus** — there is no signature database. Point `DOCUMENT_SCANNER` at ClamAV or a
  cloud scanner for real coverage. Quarantined files are never previewed.
- Media is **never served statically**. Bytes are reachable only through the
  authenticated, audited download endpoint; S3 storage uses private ACLs and never hands
  out bucket URLs.
- Previews are fetched with the session and rendered from `blob:` URLs whose type is
  pinned from the validated extension, never from the uploader-declared MIME type.
- Images are built with a `.dockerignore` that excludes `backend/media`, database files,
  `.env` and virtualenvs. **Images built before it existed contain whatever was in
  `backend/media` on the build machine** — rebuild them and do not push old ones.

## AI security

Uploaded documents are untrusted input to a language model.

- The prompt separates rules, passages and the question; passages are declared to be data,
  never instructions. Passages containing instruction-like text are flagged, reported to
  the user, and **never quoted as an answer** by the extractive answerer.
- Every generated answer is **verified before it is returned**: each citation must resolve
  to a supplied passage, the answer must cite something, **every figure it states must
  appear in a passage it cites**, and its prose must be substantially drawn from those
  passages. Any failure turns the answer into an abstention.
- Retrieval is permission-filtered first, so a model can only ever see passages the user
  could open. There are no tools, no browsing and no write actions available to any model.
- Document content is **not sent to an external provider** unless an operator configures
  one. The OpenAI-compatible presets record whether the provider's free tier may train on
  submitted content, and log a warning when it may.
- Answer rendering never produces HTML from a string (see
  [frontend/README.md](../../frontend/README.md)), and the CSP blocks inline and foreign
  script as a second line of defence.

## Transport and headers

TLS terminates at nginx (TLS 1.2+). Django redirects to HTTPS outside DEBUG, trusts
`X-Forwarded-Proto` from nginx, and sets secure cookies. HSTS is off until an operator sets
`SECURE_HSTS_SECONDS` (it is near-irreversible, so it is a domain owner's decision). The
frontend is served with a strict Content-Security-Policy, `X-Frame-Options: DENY`,
`nosniff`, `Referrer-Policy: same-origin`, a restrictive `Permissions-Policy` and COOP
(`infrastructure/nginx/security_headers`). CORS credentials are off; the application is
same-origin.

## Audit

Recorded with actor, action, result (allowed or denied), document and metadata: every
document read, download (with version), write, archive, restore, reprocess and version
upload; search and chat (never the query or question text); login, failed login (against
the targeted account, or the attempted username if none exists), logout, password changes;
account and department changes (with before/after of role, department and activity); job
retries. Denials are recorded as well as approvals.

## Logging

Structured JSON through one formatter that redacts document content whatever the caller
passed. Metric labels are bounded vocabularies — never ids, users or query text.
Health and readiness responses carry statuses and latencies, never hosts or error
messages.

## Known gaps

| Gap | Why it matters | Status |
|---|---|---|
| No antivirus engine | The heuristic scanner has no signatures | Integration point exists (`DOCUMENT_SCANNER`) |
| No encryption at rest in the application | Relies on disk/volume or bucket encryption | Enable on the storage layer |
| No SSO / MFA | Institutional identity is usually federated | Not built |
| Conversation history keeps citation text after access is revoked | It is a record of what that user was shown; a department change does not retract it | By design; revisit if policy requires |
| Hosted providers never exercised live | Contract-tested only | Unverified |
| Dependency audits are advisory in CI | A new CVE flags, does not block | By design |
