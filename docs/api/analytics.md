# Analytics, feedback review and dashboard activity

Three read models over records the platform already keeps - assistant answers, feedback,
processing runs and their stage events, uploaded versions, and the audit log. No language
model is called and no question or search text is read to build them. Every endpoint is
served at `/api/v1/` and at the `/api/` alias. Code: `backend/documents/operations/analytics.py`
and `feedback.py`; operator notes in [../operations/analytics.md](../operations/analytics.md).

## Scope (all three)

The same boundary as every administration view (`operations.views._department_scope`):

| Data | System administrator | Department administrator |
|---|---|---|
| Answers, latency, abstentions, feedback | everything | conversations of their department (`Conversation.department`) |
| Documents, uploads, storage, processing runs | everything | documents of their department |
| Active people, searches | everything | actions by members of their department |
| Document views (most read) | everything | views of their department's documents |
| Documents named in rankings | any | only their department's - others are left out, never named |

Nothing in a report depends on which administrator of a scope asked, so one cached report
serves them all.

## `GET /api/v1/admin/analytics/`

Administrators only (`IsAdministrator`; others **403**). Query: `range` = `7d` | `30d`
(default) | `90d` (anything else **400** `{"range": [...]}`); `fresh=1` recomputes instead of
serving the cached copy and is throttled per user (`analytics_refresh`, default 60/hour,
`THROTTLE_ANALYTICS_REFRESH`); a cached read is limited only by the general `user` rate.
Reports are cached per scope and range for `ANALYTICS_CACHE_SECONDS` (300).

Days are calendar days in `ANALYTICS_TIME_ZONE` (default `TIME_ZONE`, UTC). The current period
is the last `days` days including today; the previous period is the `days` days before it.
**Every daily series has exactly `days` entries, oldest first, gap-filled**: 0 for a count,
`null` for a rate or percentile with nothing to measure. Dates are `"YYYY-MM-DD"` strings.

```jsonc
{
  "range": {
    "key": "30d", "days": 30,
    "start": "2026-10-09", "end": "2026-11-07",           // current period, inclusive
    "previous_start": "2026-09-09", "previous_end": "2026-10-08",
    "time_zone": "UTC"
  },
  "generated_at": "2026-11-07T10:02:11.123Z",
  "cache_seconds": 300,
  "scope": { "department": null },                        // null = all departments
  "kpis": {
    // Each KPI: {value, previous, delta, unit, series}.
    //   unit "count" | "ratio" (0-1) | "bytes"
    //   delta = value - previous (a ratio's in fraction points: 0.03 = 3 points),
    //   null when either side is null. series: one value per day.
    "questions":           { "value": 426, "previous": 290, "delta": 136, "unit": "count", "series": [12, 9, ...] },
    "answer_rate":         { "value": 0.78, "previous": 0.79, "delta": -0.011, "unit": "ratio", "series": [0.75, null, ...] },
    "helpful_rate":        { ... },   // HELPFUL / all ratings given in the period
    "documents_processed": { ... },   // runs that finished COMPLETED
    "documents_failed":    { ... },   // runs that finished FAILED
    "active_users":        { ... },   // distinct people with an audit row (failed sign-ins excluded);
                                      // value/previous are distinct over the whole period, series per day
    "searches":            { ... },
    "zero_hit_rate":       { ... },   // searches with 0 hits / searches
    "storage_bytes":       { ... }    // value: bytes stored now (every version); previous: at the
                                      // start of the period; series: at the end of each day
  },
  "questions": {
    "series": [
      { "date": "2026-10-09", "total": 12, "answered": 9, "general": 1, "abstained": 2,
        "generative": 5, "quoted": 4 }
      // answered = from the documents (generative + quoted); general = general-knowledge
      // replies; abstained = not answered. total = answered + general + abstained.
    ]
  },
  "latency": {
    "series":  [ { "date": "2026-10-09", "p50_ms": 3120, "p95_ms": 14800, "answers": 12 } ],
    "overall": { "p50_ms": 3120, "p95_ms": 14800, "answers": 400 },
    "stages":  [ { "stage": "retrieval", "median_ms": 640, "answers": 400 } ]
    // Answers with a recorded duration (duration_ms > 0). stages: median of each key in
    // Message.metadata.timings (retrieval, rerank, generation, grounding, then any other).
  },
  "abstentions": [
    { "code": "not_relevant", "reason": "retrieved passages were not relevant enough", "count": 31 }
    // code: no_documents | not_relevant | no_context | nothing_found | answerer_failed |
    //       ungrounded | in_figure | parts_unanswered | empty_question | other.
    // reason: the most frequent recorded reason with that code (feed it to the frontend's
    // explainAbstention()). Most frequent first.
  ],
  "feedback": {
    "series": [ { "date": "2026-10-09", "helpful": 2, "not_helpful": 0, "incorrect": 1 } ],
    "totals": { "helpful": 50, "not_helpful": 7, "incorrect": 3 },
    "flagged": [ /* up to 8 FeedbackItem (below), INCORRECT or NOT_HELPFUL, newest first */ ]
  },
  "documents": {
    "uploads":    [ { "date": "2026-10-09", "count": 3, "bytes": 5242880 } ],  // versions uploaded
    "processing": [ { "date": "2026-10-09", "completed": 4, "failed": 1 } ],   // runs by the day they finished
    "failures":   [ { "error_code": "OCR_FAILED", "count": 4 } ],             // FAILED runs in the period
    "stages":     [ { "stage": "OCR", "p50_seconds": 14.2, "p95_seconds": 61.5, "runs": 140 } ],
                  // SUCCEEDED stage events in the period, in pipeline order; skipped stages excluded
    "by_type":       [ { "document_type": "POLICY", "count": 64 } ],          // non-archived documents now
    "by_department": [ { "department": "Physics", "count": 52 } ],
    "storage": {
      "total_bytes": 907000000, "files": 212,                                   // every version kept
      "by_department": [ { "department": "Physics", "bytes": 300000000, "files": 70 } ],
      "by_kind":       [ { "kind": "PDF", "bytes": 700000000, "files": 150 } ]  // PDF | DOCX | TXT | IMAGE | OTHER,
                                                                               // by file-name extension
    }
  },
  "usage": {
    "active_users": [ { "date": "2026-10-09", "count": 14 } ],
    "searches":     [ { "date": "2026-10-09", "count": 22, "zero_hit": 3 } ],
    "top_cited":    [ { "document_id": 1, "title": "Academic Regulations", "answers": 41, "citations": 77 } ],
                    // up to 10; answers = distinct answers citing it in the period
    "most_viewed":  [ { "document_id": 1, "title": "Academic Regulations", "readers": 34, "visits": 61 } ]
                    // up to 10; readers = distinct people with an allowed DOCUMENT_VIEW,
                    // visits = distinct person-days (opening a document records several views)
  }
}
```

## Feedback review queue

`GET /api/v1/admin/feedback/?rating=INCORRECT,NOT_HELPFUL&since=2026-10-01&page=2` -
administrators only, scoped as above, newest first, paginated like every list
(`{count, next, previous, results}`, 50 a page). `rating`: one value or several,
comma-separated, case-insensitive (unknown **400**); `since`: a date (that day's start) or an
ISO datetime (malformed **400**). Not audited (a read of aggregate quality data, like the
other monitors).

A `FeedbackItem`:

```jsonc
{
  "id": 9,
  "rating": "INCORRECT",                     // HELPFUL | NOT_HELPFUL | INCORRECT
  "comment": "The curfew changed to 11 pm.", // what the rater wrote for the reviewer
  "created_at": "2026-11-07T08:01:00Z",      // when it was rated
  "message_id": 412,
  "answered_at": "2026-11-07T07:58:00Z",
  "department": "Computer Science",          // the conversation's
  "question": "What time is the hostel curfew?",  // see "Question text" below; null = not shown
  "answer_excerpt": "Residents must return ...",  // <= 280 characters, cut at a word
  "answer_withheld": false,
  "abstained": false,                        // as judged (snapshot taken with the rating)
  "abstention_reason": "",
  "citations": 2,                            // as judged
  "source": "documents",                     // documents | general | none
  "generative": true,
  "model": "gemini/gemini-2.5-flash",        // model_version, else model_name
  "support_ratio": 1.0,
  "documents": [ { "id": 2, "title": "Hostel Rules" } ]  // cited documents the reader may see, up to 3
}
```

**Question text.** Shown only to a **system administrator**, and only on a **flag**
(INCORRECT or NOT_HELPFUL): the rater asked for that answer to be looked at, and the
operator who can already read every document is the one who looks. It is `null` for a
department administrator (often the asker's own head of department) and on every HELPFUL
rating. `""` means a flag whose question could not be found. The audit log still never
holds question text, and the analytics aggregates never read it.

**Who rated** is never returned, to anyone: the queue judges answers, not people.

**Withheld answers.** For a department administrator, an answer that cites a document
outside their department - or one since deleted, whose department can no longer be
checked - has `answer_withheld: true`, an empty `answer_excerpt`, and those documents left
out of `documents`: an excerpt quotes them.

## Chat messages: `my_feedback`

Every message in `GET /api/v1/chat/conversations/{id}/` carries
`my_feedback: {"rating": "INCORRECT", "comment": "..."} | null` - the conversation owner's own
rating (the owner is the only person who can read or rate it), so a rated answer stays rated
when the conversation is reopened. One query for the whole conversation.

## Dashboard activity

`GET /api/v1/dashboard/` gains an additive `activity` block for the signed-in user, cached
per user for `DASHBOARD_ACTIVITY_CACHE_SECONDS` (60); `org` is cached per scope for the same
time. Uploads and composition are over `visible_documents` (non-archived) - exactly what the
reader may open; questions are the reader's own.

```jsonc
"activity": {
  "generated_at": "2026-11-07T10:02:11Z",
  "time_zone": "UTC",
  "uploads_by_week":  [ { "week": "2026-08-17", "count": 3 } ],   // 12 entries, oldest first;
                                                                  // `week` is the Monday;
                                                                  // versions uploaded to visible documents
  "questions_by_day": [ { "date": "2026-10-09", "count": 2, "answered": 1 } ],  // 30 entries;
                                                                  // answered = from the documents
  "documents_by_type":       [ { "document_type": "POLICY", "count": 12 } ],
  "documents_by_department": [ { "department": "Physics", "count": 30 } ],
  "org": null                // for anyone without view_operations, else:
  // "org": {
  //   "range": { ...as above, "key": "7d", "days": 7 },
  //   "generated_at": "...",
  //   "scope": { "department": "Physics" },       // null for a system administrator
  //   "kpis": { "questions": Kpi, "answer_rate": Kpi, "helpful_rate": Kpi,
  //             "documents_processed": Kpi }      // Kpi as above, 7-day series
  // }
}
```

## Evaluation runs

`/api/v1/evaluations/` (list, detail, `{id}/results/`, `{id}/compare/{other}/`) now requires
an **administrator** - a benchmark's questions and expected answers are its department's
answer key. Scoping by the dataset's department is unchanged (a department administrator
sees their department's datasets; a department-less one nothing). Each run's
`dataset.cases` is counted with the dataset in one query (it was one query per row).
