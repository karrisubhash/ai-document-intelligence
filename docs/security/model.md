# Security model

## Identity

- **Browser sessions** are httpOnly, `Secure` (outside DEBUG), `SameSite=Lax` cookies,
  cached in Redis with a database copy, with an **8-hour absolute lifetime**
  (`SESSION_COOKIE_AGE`). Login rotates the session key (no fixation) and **enforces CSRF
  even though the caller is anonymous** — DRF would otherwise exempt it, enabling forged
  logins into an attacker's account. No credential is readable by JavaScript.
- **API tokens** (scripts, services) expire after `TOKEN_TTL_HOURS` and are deleted on
  first use after expiry. Signing in again replaces a token with less than half its life
  left, so a re-issued token is never seconds from expiry. Logout with a token deletes it.
- **Passwords** use Django's validators (length, similarity, common, numeric). A new
  password however it is set - the API, Django's admin, `manage.py changepassword` -
  invalidates every other session (the session hash changes) and deletes every API token
  (a model signal; Django's own hash upgrades on login are not treated as changes).
- **Deactivation** stops sign-in, **deletes the account's sessions** (an inactive account's
  session is otherwise only refused, and came back on reactivation) and deletes its
  tokens, whichever way it is deactivated. Accounts are never deleted through the API, so
  their audit history keeps its subject.
- **Brute force** is limited three ways: per address *and* account (10/min on the token,
  login and password endpoints; the username normalised as authentication sees it, so
  padding it with spaces does not reset the count), per **account across all addresses**
  (100/hour, the NIST SP 800-63B ceiling, against a guessing run spread over a botnet),
  and at nginx per address. Password changes are throttled per signed-in user. Django's
  admin login (`/django-admin/login/`) is limited at nginx and its failures are audited
  like the API's.
- **Nobody can take over a more powerful account**: a role-only system administrator
  cannot reset or edit a superuser, and a department administrator cannot touch a Django
  staff account. The API returns `can_manage` per account so the screens offer only what
  will succeed.
- **Self-service sign-up** (`SIGNUP_ENABLED`, on by default; institutional decision I7)
  files a *request*, never an account: a pending applicant is not a `User` at all, so no
  path - Django's admin, a shell, an "Active" checkbox - can let one in early. Approval is
  the only way a request becomes an account, and it is held to the same rules as creating
  one by hand: a department admin approves only requests for their own department, into
  it. No approval grants an administrative role, even a system administrator's; an
  account that needs one is promoted afterwards, as a separate, audited edit.
  - *Identity is not verified.* The name, email and note are whatever the applicant
    typed; the app sends no mail, so nothing proves an address belongs to them. A
    reviewer must confirm who the person is some other way (a staff or student number
    checked against the institution's records, say) before approving: approval gives
    read access to the whole department's documents. The review screen labels the
    details as unchecked and names any account the reviewer administers that already
    uses the same email. `SIGNUP_EMAIL_DOMAINS` restricts requests to the institution's
    domains - it narrows who can ask; it does not prove who is asking.
  - *Usernames* are NFKC-normalised and must be ASCII, so a stranger cannot choose a
    look-alike of an existing name (a fullwidth or Cyrillic copy of an administrator's
    username); a name equal to an account's or a waiting request's under either of
    PostgreSQL's case rules is taken. The form says whether a name is taken - inherent in
    letting people choose one - with the same answer for an account and a waiting request.
  - *Passwords* are validated and kept only as a hash. Approval hands the hash to the
    account and clears it from the request. A declined request keeps its hash so that
    sign-in can tell that applicant why they cannot get in - until an account takes the
    name, however it is made, which clears every request's hash for that name. Declined
    requests, with their details, are otherwise kept indefinitely (a retention decision,
    like D3).
  - *Sign-in* tells an applicant their request is waiting or declined only for the exact
    username and the right password, and a failed sign-in costs the same one extra hash
    whoever holds the name, so neither the answer nor its timing says who has applied.
  - *Abuse:* CSRF is enforced; requests are throttled per address (100/hour by default,
    counted only once CSRF, the on/off switch and the queue check have passed, so a
    hostile page cannot spend a campus's allowance) and at nginx (POSTs only); and at
    `SIGNUP_MAX_PENDING` waiting requests no more are accepted - for anyone, the price of
    the bound - until administrators clear the queue, which the audited bulk decline does
    in a few calls. The Django admin cannot delete requests.
  - With sign-up on, the active departments' names, codes and ids are public, because the
    form offers them.

- **The one-click demo** (`accounts/demo.py`, off unless `DEMO_ENABLED=True`). Each visitor
  who presses "Try the live demo" gets an account of their own - never a shared login, so
  the owner-scoped conversation rules keep every visitor's questions from every other
  visitor. What makes such an account safe to hand to anyone:
  - *No credential but the session.* An unusable password and a random `guest-…` name: it
    can never be signed into again, by its visitor or anyone else. No token can be issued
    to it, and `ExpiringTokenAuthentication` refuses (and deletes) one made by hand.
  - *Read-only, enforced in one place.* `accounts.middleware.GuestReadOnlyMiddleware`,
    after authentication, refuses every request from a guest's session that is not GET,
    HEAD or OPTIONS - before any view runs - unless its URL name is on a short allowlist:
    ask (`chat`, `chat-stream`), `search` (a POST only to keep queries out of URLs),
    rate an answer (`message-feedback`), mark notifications read (matched by the
    notifications API's documented paths), sign out, and the public doors (`auth-login`,
    `auth-demo`, `auth-signup`, `api-token-auth`, `auth-csrf`), which work on the
    credentials or form they are sent and never on the signed-in account. Matching on URL
    names holds for `/api/v1/` and `/api/` alike; an endpoint added later is closed to
    guests until someone adds it. A refusal is counted (`authz_denials{action="GUEST_WRITE"}`)
    and logged, not audited - a row per refusal would let a script write to the audit log
    as fast as it can send.
  - *The least access there is.* STUDENT in `DEMO_DEPARTMENT`: it reads that department's
    documents (the sample library) and its own conversations, owns nothing, curates
    nothing and sees no administration screen. Guests are not administrable - the user
    endpoints neither list them nor accept a password or role for one - so nobody can turn
    a guest into an account that signs in or reads more.
  - *Bounded cost.* Demo starts are throttled per address and across all addresses, and
    `DEMO_MAX_GUESTS` caps the guests existing at once (creation is serialised on the demo
    department's row, so the cap is exact). Guests' chat and search spend per-guest and
    all-guest budgets on top of the views' own throttles; members are never counted
    against them, so a busy demo cannot slow the people the installation is for.
  - *A ceiling nobody can hold shut.* A start needs only a CSRF cookie, and addresses are
    cheap, so the cap must not be reachable by guests nobody uses. At the cap a new visitor
    takes the place of the guest idle longest, once that is `DEMO_GUEST_IDLE_MINUTES`
    (each guest request is noted, to within five minutes, in `last_seen_at`; one that never
    made a request counts from its start): a script that starts demos and walks away holds
    each place for minutes, not until it expires. And `THROTTLE_DEMO_GLOBAL` ×
    `DEMO_GUEST_TTL_HOURS` (60/hour × 3 h = 180) stays within the cap (200), so not even
    guests kept busy can fill it; `manage.py check` warns about settings that break either
    bound (`accounts.W001`, `accounts.W002`).
  - *Ephemeral.* `manage.py purge_demo_guests` (and a sweep of up to 20 on every new demo)
    deletes guests older than `DEMO_GUEST_TTL_HOURS` with their conversations, messages,
    ratings, notifications and tokens; the delete re-checks the guest flag, so nothing else
    can be removed. Their audit rows stay with the account cleared, as for any deleted
    account; each sweep that deleted something writes one `GUEST_PURGE` row, whose
    `reason` says which guests went (`expired`, or `idle` at the cap). A guest's
    session never outlives its account.
  - The demo is offered only once its department holds a Ready document, and the sample
    library's documents belong to an inactive `sample-library` account, so a visitor never
    learns an administrator's username from "owner" or "uploaded by". Owning a document
    grants reading and managing it in every department, so that name is reserved for sign-up,
    account creation and approval (compared NFKC-normalised and case-folded, as the `guest-`
    prefix is), and the samples are never given to an existing account that can sign in or
    has a role or department - loading them refuses instead.
  - *Not covered:* guests' questions use whatever answerer is configured. With a hosted
    model, the all-guest chat budget is what bounds how much of its quota visitors can use.

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
- The **heuristic scanner** (enable with `DOCUMENT_SCANNER`) runs before any parser. It
  **quarantines** disguised executables (PE, ELF, Mach-O), scripts that start with a `#!`
  interpreter line and the EICAR string. Office
  macros and PDF active content (`/JavaScript`, `/OpenAction`, `/Launch`, embedded files)
  are only **flagged**: the file is marked suspicious, the findings are recorded, and it is
  still processed and downloadable. **It is not an antivirus** — there is no signature
  database. Point `DOCUMENT_SCANNER` at ClamAV or a cloud scanner for real coverage.
  Quarantined files are never previewed, never processed again (reprocess and retry are
  refused; a file the scanner was wrong about is uploaded as a new version), and the
  download endpoint refuses them (403) to everyone but a system administrator. This is
  judged per version, so an earlier infected upload stays refused after a clean one
  replaces it.
- **Before its scan finishes, a new upload can be downloaded** by everyone who can read
  it. Scanning runs in the pipeline, not in the upload request. See the known gaps.
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
upload; search and chat (never the query or question text); login, failed login through
the API or Django's admin (against the targeted account; for a name that matches no account
only a keyed fingerprint, because the username box is where people type their password by
mistake), logout, password changes;
account and department changes (with before/after of role, department and activity);
sign-up requests, approvals and declines (`SIGNUP_REQUEST`, `SIGNUP_APPROVE`,
`SIGNUP_REJECT` - an account made by approval has a `SIGNUP_APPROVE` row, not
`USER_CREATE`; refused sign-up attempts are not recorded); job retries. Denials are recorded as well as approvals, including on reads that are not
audited when allowed (the processing timeline, which the screen polls). A department
administrator sees their members' denied attempts on other departments' documents without
the document's id or title - a denied row exists only for an id that exists, so the id
alone would reveal which ids are real.

Deleting a document does not erase its trail. Every row about a document keeps the
document's id and department. The delete row also keeps its title, so the trail still
answers what was deleted, by whom, and who read it before. The title therefore outlives
the document in the audit table. Whether that is acceptable, and for how long audit rows
are kept, is a retention decision for the institution; nothing prunes the table today.

## Logging

Structured JSON through one formatter that redacts document content whatever the caller
passed. Metric labels are bounded vocabularies — never ids, users or query text.
Health and readiness responses carry statuses and latencies, never hosts or error
messages.

## Known gaps

| Gap | Why it matters | Status |
|---|---|---|
| No antivirus engine | The heuristic scanner has no signatures | Integration point exists (`DOCUMENT_SCANNER`) |
| A new upload is downloadable before its scan completes | Scanning is asynchronous; a flagged file is quarantined only once a worker reaches it | Institutional decision: holding every file until scanned would also hold back previews of every document in processing |
| Redis has no password | Read access to Redis exposes session keys; it is reachable from every container on the compose network, not from outside | Accepted for a single host; required before any cross-host deployment |
| `/django-admin/` is reachable from the internet | Password-only superuser login, limited to 1 request/s per IP at the edge | Institutional decision: restrict to an admin network or VPN |
| No encryption at rest in the application | Relies on disk/volume or bucket encryption | Enable on the storage layer |
| No SSO / MFA | Institutional identity is usually federated | Not built |
| Conversation history keeps citation text after access is revoked | It is a record of what that user was shown; a department change does not retract it | By design; revisit if policy requires |
| Hosted providers never exercised live | Contract-tested only | Unverified |
| Dependency audits are advisory in CI | A new CVE flags, does not block | By design |
