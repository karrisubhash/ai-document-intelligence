# Notifications and attention

The bell in the top bar, the badges on the sidebar's **Users** and **Processing**
items, and the backend app behind them (`backend/notifications/`). API shapes are in
[docs/api/reference.md](../api/reference.md#notifications).

## Why

Before this, the product was silent: an upload disappeared into *Processing* with no
news when it finished or failed, an administrator working in Documents never learned
that someone was waiting for approval, and an approved applicant was never told. The
pending-sign-up count was fetched only by the Dashboard and the Users page.

## The model

`notifications.Notification(recipient, kind, title, body, url, payload, dedupe_key,
created_at, updated_at, read_at)`.

- **Text is stored as composed** - a notification says what was true when it was sent.
  It is plain text (the client renders it as text), cleaned by the service: control
  characters removed (a NUL is a PostgreSQL error), titles on one line, everything cut to
  its column with an ellipsis rather than refused.
- **`url` is an in-app path** (`/documents/12?tab=processing`). Absolute,
  protocol-relative (`//host`, `/\host`) and `javascript:` links are refused with
  `ValueError` - a notification is the last place an open redirect should live.
- **`payload`** is producer bookkeeping (document ids, outcomes already announced). It is
  never serialized to the client.
- **`updated_at`** is when the news last changed - set by the service, deliberately not
  `auto_now`: marking read is not news, and the client watches the newest `updated_at`
  (`summary.latest_at`) to tell that something arrived.
- Indexes: `(recipient, -updated_at, -id)` for the feed and the summary's newest time (it
  also replaces the foreign key's own index); a partial index on `recipient` where unread
  for the polled count and read-all; `updated_at` for retention. A partial **unique**
  constraint allows one *open* notification per `(recipient, dedupe_key)`.

## The service (`notifications.service`) - what other features call

```python
notify(recipient, kind: str, title: str, *, body: str = "", url: str = "",
       payload: dict | None = None, dedupe_key: str | None = None) -> Notification

coalesce(recipient, dedupe_key: str, build: Callable[[Notification | None], Content | None],
         *, window: timedelta | None = None) -> Notification | None

mark_read(recipient, notification_id) -> bool        # False: not theirs / no such id
mark_all_read(recipient, *, until=None) -> int
```

- `kind` is a short dotted lower-case name (`document.ready`); `ValueError` otherwise.
- With a `dedupe_key`, an **open** notification - unread and still holding the key - is
  updated in place (text replaced, `updated_at` moved to now) instead of duplicated.
  Reading it closes it: the next news with that key starts a new one.
- `coalesce()` is the general form: `build(previous)` receives the open notification
  (row-locked) or `None` and returns the `Content` to store, or `None` for "nothing to
  add" (how producers stay idempotent). With a `window`, an open notification not updated
  within it is closed (it keeps its text) and the news starts a fresh one.
  `Content(fresh=True)` closes the open one too - a producer whose notification is full.
- Two processes opening the same key at once: the unique constraint refuses the second
  insert, and `coalesce` retries once, finding the first and merging into it.
- Nothing in the service audits: a notification is the consequence of something audited
  where it happened (an upload, an approval), and reading one is not an action anyone
  needs a trail of.

## Producers

Producers listen to other apps' models with `post_save` receivers connected in
`NotificationsConfig.ready()` - the pipeline and the sign-up services are not edited and
do not know anyone is told anything. Every receiver defers its work with
`transaction.on_commit` and runs it through `processing.safely`, which logs and never
raises: a notification that fails to write must not roll back or fail the run, approval
or sign-up that caused it, and one written for a transaction that then rolled back would
announce something that did not happen.

### Processing results (`notifications/processing.py`)

Every terminal transition the pipeline makes is a save of the `ProcessingJob` with
`status` in `update_fields` (`pipeline._mark_completed`, `_mark_failed`,
`services._mark_stalled`). On COMPLETED or FAILED (quarantine is FAILED with
`MALWARE_DETECTED`):

- **Only an outcome that speaks for the document**: the job is for the document's
  *current* version and no later job for that version exists - the same rule as
  `pipeline._set_document_status`. A run on a superseded version, or a stalled run that
  Reprocess already replaced in the same transaction, is not news. Decided from the job,
  never from `Document.status`: `_mark_failed` runs in autocommit in a worker, so the
  announcement runs before the document's status is written
  (`AutocommitTests` pins this). Archived documents are not announced.
- **To the owner and the uploader of that version**, each only if active and still able
  to read the document (`authorization.can_view_document`) - a title is content.
- **Once per job and outcome.** Each notification records `"<job id>:<status>"` events;
  a Celery redelivery that runs a failed job again, or any second save of a finished
  job, finds its event already recorded (in any of the recipient's processing
  notifications, read or not) and sends nothing. The task's own transient retries never
  get here - they are recorded as QUEUED (`pipeline._mark_retrying`).
- **Coalesced**: results arriving within `NOTIFICATIONS_COALESCE_MINUTES` (10) of the
  last, while unread, fold into one notification - "12 documents are ready", "11
  documents are ready, 1 could not be processed". One entry per *document*: a document
  that failed and then succeeded on a reprocess reads as ready, once. At 50 documents a
  notification is full and the next one starts a new notification. `0` disables
  coalescing (each document its own notification, still updated in place while unread).
- **Plain reasons**: the error code becomes words a person can act on ("the file appears
  to be damaged", "no readable text was found in it", "processing was interrupted");
  `error_message` - which can carry paths and host names - is never shown. The body
  says what to do: upload a new version, reprocess, or (quarantine) upload a clean copy.
- Links: one document - `/documents/{id}` (`?tab=processing` when it failed); several -
  `/documents`, or `/documents?status=FAILED` when all of them simply failed.

**Not covered**: `services.enqueue` marks a job FAILED with a queryset `UPDATE` when the
broker refuses the message; no signal fires for that, so it is not announced (it shows on
the Processing screen and in the attention count).

### Sign-ups (`notifications/signups.py`)

- **A new request** notifies exactly the administrators who may decide it - decided by
  `accounts.permissions.reviewable_signups` itself for each active candidate: every system
  administrator and superuser, and the department administrators of the department the
  request names. Requests coalesce while unread: "3 people asked for an account". The
  notification carries the applicant's name, the department and role asked for - not
  their email address or note, which stay on the review screen.
- **A decision** (approve, reject, bulk reject, Django admin - all go through
  `approve_signup`/`reject_signup`, which save the row) settles those notifications: once
  every request an unread one mentions has been decided, by anyone, it is marked read.
- **An approval** welcomes the new account once (`account.welcome`), linking to the
  dashboard: the first thing in their bell when they first sign in. A declined applicant
  has no account to notify.

## Attention counts (`notifications/attention.py`)

`GET /api/admin/attention/` gives the sidebar badges: `pending_signups`,
`failed_documents` (quarantined included), `quarantined_documents`, `stalled_jobs`, scoped
like the operations views (a department administrator: their department's documents;
archived documents not counted). Cached per person for
`NOTIFICATIONS_ATTENTION_CACHE_SECONDS`; a version number in the cache key is bumped after
the commit of any sign-up request save and of any job creation or terminal transition,
so a decision or a retry shows on the next poll. A cache outage still answers (computed).

## The client

- `src/components/NotificationBell.tsx`: polls `summary` every 60 s while the tab is
  visible, on window focus and when the tab becomes visible again (folded so a pair of
  events is one call), never while hidden. When `latest_at` moves it fetches the first
  page, announces the new items to screen readers (a polite live region) and dispatches
  them to open screens (`useNotificationArrivals` in `src/api/notifications.ts`; the
  documents list refreshes on `document.*` news). The panel is a non-modal dialog:
  focus moves into it, Escape closes it and returns focus to the bell, a click outside
  or tabbing away closes it. A click on a notification marks it read and follows its
  link; "Mark all as read" sends `until` = the newest time shown.
- `Layout.tsx` sidebar: `NavItem.badge` (`navigation.ts`) puts a count on Users and
  Processing, a dot on the collapsed rail. The links keep their accessible names
  ("Users", "Processing" - the e2e specs pin them); the count is in an `aria-hidden` pill
  and is the link's accessible *description* ("2 sign-up requests waiting").

## Operations

- `python manage.py prune_notifications` - run daily from cron. Deletes read
  notifications older than `NOTIFICATIONS_RETENTION_DAYS` (30) and unread ones older
  than `NOTIFICATIONS_UNREAD_RETENTION_DAYS` (180), by `updated_at`, in batches;
  `--dry-run`, `--read-days N`, `--unread-days N`, `0` keeps that kind forever.
- Django admin lists notifications read-only, for "I was never told" questions.
- No email is sent yet: the SMTP settings exist, but sign-up decisions are only told in
  the app.
