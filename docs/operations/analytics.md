# Analytics

The **Analytics** screen (`/admin/analytics`, first item under Administration) shows what the
platform has been doing, from its own records. It is for administrators: a department
administrator sees their department, a system administrator the whole installation. The
endpoints and exact shapes are in [../api/analytics.md](../api/analytics.md).

Grafana (loopback only, reached over an SSH tunnel) stays the operator's tool for the
running system - request latency, queue depth, process memory. This page answers the
product questions without a tunnel: are people using it, are answers good, are documents
getting through.

## What the page shows

| Tab | Content |
|---|---|
| Overview | Questions asked, share answered from the documents, share rated helpful, active people, documents processed, processing failures, searches, searches with no results - each with its change against the previous period of the same length and a daily sparkline. Questions per day by outcome (answered from documents / not answered / general knowledge), answer time (median and slowest 5%), processing runs per day |
| Answers & quality | Answer rate per day; answers written by the model vs quoted; why questions went unanswered (the same plain explanations the chat shows); feedback per day; median time of each answer step (finding, ranking, writing, checking); the **rated answers** queue (flagged by default); the latest benchmark run's scorecard against the run before it, a trend across runs, and the run list |
| Documents | Files uploaded, runs completed and failed, storage and its growth; throughput and uploads per day; why processing failed, in plain words (with a link to Processing to retry); typical and slowest time per pipeline stage; the library by type and by department; storage by file kind and department |
| Usage | Active people per day; searches per day and how many found nothing; most cited documents (by how many answers cited them) and most read documents (by distinct readers; a visit is one person on one day) |

The period is 7, 30 or 90 days, and the period and tab are in the address, so a view can be
bookmarked. A new installation shows a note that the charts fill in as the library is used,
and every chart has its own empty message rather than a flat line.

How the numbers are counted, where it is not obvious:

- **A question** is one assistant answer. Answered = from the documents; *general knowledge*
  replies (only with `CHAT_GENERAL_KNOWLEDGE`) and abstentions are counted separately, so the
  answer rate is the share the documents could answer.
- **Documents processed / failed** count processing *runs* by the day they finished; a
  reprocessed document counts again.
- **Active people** have an audit row in the period - signed in, opened a document, searched,
  asked, or administered. A failed sign-in is recorded against the account it targeted and is
  excluded. Browsing the document list leaves no row, so this slightly undercounts.
- **Stage durations** use stages that ran; a skipped stage (OCR on a text PDF) is excluded so
  it cannot make OCR look fast.
- **Storage** is every stored version of every document, archived ones included; its history is
  the growth of the files that exist now (a deleted document leaves every day's figure).

## Privacy

- **No question or search text** is read to build any chart: searches are counted from the
  `SEARCH` audit rows (which hold only the number of hits), questions from their recorded
  outcome. The audit log still never records either.
- The **rated answers** queue never says who rated, and dates a rating to the day only: every
  answer has an audit row under the asker's name from the same moment, so an exact time
  would say who rated it. It shows the answer as an excerpt, and to a
  department administrator withholds it when the answer quotes a document outside their
  department. The **question** is shown only to system administrators and only for answers
  flagged incorrect or not helpful - the rater asked for that answer to be looked at, and the
  operator who can already read every document is the one who looks. Department
  administrators never see question text.
- Rankings name only documents in the reader's scope.

## Caching and cost

One report is about twenty indexed aggregate queries over at most twice the period (the
previous period is the comparison). It is cached in the Redis cache per department scope and
period for `ANALYTICS_CACHE_SECONDS` (default 300); the page says how old it is. **Refresh**
recomputes it, throttled per administrator (`THROTTLE_ANALYTICS_REFRESH`, 60/hour; past it,
the page keeps showing the cached figures and says so). The queries are backed by the indexes
of migration `documents 0022_analytics_indexes` (answers, feedback, runs and stage events by
time, uploads by time, audit rows by action and time). Building them takes a brief write lock
on those tables; on the pilot's data volume that is well under a second.

The dashboard's trends (`activity` on `GET /api/v1/dashboard/`) are cached per user, and an
administrator's "this week" numbers per scope, for `DASHBOARD_ACTIVITY_CACHE_SECONDS` (60) -
the dashboard polls while documents process.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ANALYTICS_TIME_ZONE` | `TIME_ZONE` (UTC) | Where a day begins for every daily series, e.g. `Asia/Kolkata`. An unknown name stops startup |
| `ANALYTICS_CACHE_SECONDS` | 300 | How long one report is served before it is computed again |
| `DASHBOARD_ACTIVITY_CACHE_SECONDS` | 60 | The dashboard's activity block, per user (and `org` per scope) |
| `THROTTLE_ANALYTICS_REFRESH` | 60/hour | Forced recomputations (`fresh=1`) per administrator |

## Evaluation runs

The benchmark endpoints (`/api/v1/evaluations/`) are now for administrators only; the page's
benchmark section reads them. Runs are started by an operator with
`manage.py evaluate`; the page only displays them.
