"""The analytics read model: what the platform has been doing, day by day.

GET /api/v1/admin/analytics/?range=7d|30d|90d[&fresh=1]    (administrators)

Every number here is an aggregate over rows the platform already writes for
its own reasons - assistant answers (Message), feedback, processing runs and
their stage events, uploaded versions, the audit log. Nothing new is
recorded to feed it, no language model is called, and no question or search
text is read: the audit log does not hold any, and the answers' statistics
come from their recorded outcome, not their words.

SCOPE. The same boundary as every administration read model
(operations.views._department_scope): a system administrator sees the whole
installation, a department administrator their department -

* answers, their latency, abstentions and feedback through the
  conversation's department (Conversation.department, stamped when it was
  created, because the answers in it were produced under that boundary);
* documents, uploads, storage and processing runs through the document's
  department;
* activity and searches through the acting user's department (its
  members), as the audit screen does; document views through the viewed
  document's department.

A document outside the scope is never named: rankings (most cited, most
viewed) list only the department's own documents. Nothing in a report
depends on who in the scope asked, which is what lets one cached report
serve every administrator of a department.

COST. One report is ~20 indexed aggregate queries over at most twice the
range (the previous period is the comparison), computed in SQL and cached
per scope and range for ANALYTICS_CACHE_SECONDS. `fresh=1` recomputes, and
is throttled ("analytics_refresh"). Days are bucketed in
ANALYTICS_TIME_ZONE, and every daily series has every day of the range,
with zero (or null, for a rate or a percentile with nothing to measure)
where nothing happened.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import CharField, Count, F, Func, Q, Sum, Value
from django.db.models.functions import Lower, TruncDate, TruncWeek
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdministrator, is_administrator
from documents import authorization
from documents.models import (
    AuditLog,
    Conversation,
    Document,
    DocumentVersion,
    Feedback,
    Message,
    ProcessingEvent,
    ProcessingJob,
)
from documents.processing.stages.ingestion import EXTENSION_TYPES
from documents.rag.engine import Abstention
from documents.throttling import BurstUserThrottle

from .feedback import (
    FLAGS,
    department_scope,
    feedback_items,
    scoped_feedback,
    with_question,
)

RANGES = {"7d": 7, "30d": 30, "90d": 90}
DEFAULT_RANGE = "30d"
# Part of every cache key: a deploy that changes a report's shape must not
# serve the previous release's cached copy for the next five minutes.
SCHEMA = 1
# Rankings (most cited, most viewed) and the flagged-answer list.
TOP = 10
FLAGGED = 8
# The dashboard's org strip always looks at the last week.
ORG_DAYS = 7

# What each recorded abstention reason is, as a stable code. Keyed by the
# engine's own constants; the planner's reason carries a count ("none of
# the 3 parts of the question...") and is matched by its wording.
ABSTENTION_CODES = {
    Abstention.NO_QUERY: "empty_question",
    Abstention.NO_DOCUMENTS: "no_documents",
    Abstention.NO_RELEVANT_PASSAGES: "not_relevant",
    Abstention.EMPTY_CONTEXT: "no_context",
    Abstention.MODEL_ABSTAINED: "nothing_found",
    Abstention.ANSWERER_FAILED: "answerer_failed",
    Abstention.UNGROUNDED: "ungrounded",
    Abstention.MAYBE_IN_A_FIGURE: "in_figure",
}

# Answer-pipeline stages as the engine times them (documents.rag.engine),
# in the order they run. Any other recorded stage follows, alphabetically.
ANSWER_STAGES = ("retrieval", "rerank", "generation", "grounding")
PIPELINE_STAGES = tuple(ProcessingJob.Stage.values)


def analytics_zone() -> ZoneInfo:
    return ZoneInfo(settings.ANALYTICS_TIME_ZONE)


@dataclass(frozen=True)
class Window:
    """`days` whole days ending today, and as many days before them.

    Today is included, so the current period's last day is still in
    progress; the previous period - the comparison for every delta - is the
    same number of whole days immediately before it.
    """

    days: int
    zone: ZoneInfo
    today: date

    @classmethod
    def ending_today(cls, days: int, now: datetime | None = None) -> "Window":
        zone = analytics_zone()
        return cls(days, zone, timezone.localdate(now or timezone.now(), zone))

    @property
    def first_day(self) -> date:
        return self.today - timedelta(days=self.days - 1)

    @property
    def dates(self) -> list[date]:
        return [self.first_day + timedelta(days=n) for n in range(self.days)]

    @property
    def previous_dates(self) -> list[date]:
        first = self.first_day - timedelta(days=self.days)
        return [first + timedelta(days=n) for n in range(self.days)]

    def midnight(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=self.zone)

    @property
    def start(self) -> datetime:
        return self.midnight(self.first_day)

    @property
    def end(self) -> datetime:
        return self.midnight(self.today + timedelta(days=1))

    @property
    def previous_start(self) -> datetime:
        return self.midnight(self.previous_dates[0])

    def day(self, field: str) -> TruncDate:
        return TruncDate(field, tzinfo=self.zone)

    def describe(self, key: str) -> dict:
        return {
            "key": key,
            "days": self.days,
            "start": self.first_day,
            "end": self.today,
            "previous_start": self.previous_dates[0],
            "previous_end": self.previous_dates[-1],
            "time_zone": str(self.zone),
        }


# --- Small helpers ------------------------------------------------------------


def _fetch(sql: str, params: dict) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _table(model) -> str:
    return connection.ops.quote_name(model._meta.db_table)


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _total(by_day: dict, days: list[date], field: str) -> int:
    return sum(by_day.get(day, {}).get(field, 0) for day in days)


def _series(by_day: dict, days: list[date], fields: tuple, empty=0) -> list[dict]:
    """One row per day, in order, with `empty` where nothing was recorded."""
    return [
        {
            "date": day,
            **{field: by_day.get(day, {}).get(field, empty) for field in fields},
        }
        for day in days
    ]


def _kpi(value, previous, series: list, unit: str) -> dict:
    """A headline number and its comparison with the previous period.

    `delta` is value - previous: a count's difference, a ratio's in
    percentage points (as a fraction: 0.03 is three points). None when
    either side has nothing to measure (a rate with no denominator).
    """
    delta = None if value is None or previous is None else round(value - previous, 4)
    return {
        "value": value,
        "previous": previous,
        "delta": delta,
        "unit": unit,
        "series": series,
    }


def _count_kpi(by_day: dict, window: Window, field: str) -> dict:
    return _kpi(
        _total(by_day, window.dates, field),
        _total(by_day, window.previous_dates, field),
        [by_day.get(day, {}).get(field, 0) for day in window.dates],
        "count",
    )


def _rate_kpi(by_day: dict, window: Window, numerator: str, denominator: str) -> dict:
    def rate(days):
        return _ratio(
            _total(by_day, days, numerator), _total(by_day, days, denominator)
        )

    return _kpi(
        rate(window.dates),
        rate(window.previous_dates),
        [
            _ratio(
                by_day.get(day, {}).get(numerator, 0),
                by_day.get(day, {}).get(denominator, 0),
            )
            for day in window.dates
        ],
        "ratio",
    )


def _as_date(value, zone: ZoneInfo) -> date:
    if isinstance(value, datetime):
        return (
            value.astimezone(zone).date() if timezone.is_aware(value) else value.date()
        )
    return value


def abstention_code(reason: str) -> str:
    if reason in ABSTENTION_CODES:
        return ABSTENTION_CODES[reason]
    if "parts of the question" in reason:
        return "parts_unanswered"
    return "other"


def _stage_order(stage: str, known: tuple) -> tuple:
    return (known.index(stage), "") if stage in known else (len(known), stage)


# --- Answers --------------------------------------------------------------------


def _answers(window: Window, department):
    """Assistant answers in scope over both periods, as a queryset."""
    messages = Message.objects.filter(
        role=Message.Role.ASSISTANT,
        created_at__gte=window.previous_start,
        created_at__lt=window.end,
    )
    if department is not None:
        messages = messages.filter(conversation__department=department)
    return messages


def _answer_days(messages, window: Window) -> dict:
    """Per day: answered from the documents (and of those, written by a
    model or quoted), answered from general knowledge, and not answered.

    Counted from positive conditions only and subtracted in Python: a
    negated JSON-key condition is NULL, not true, for a row without the key
    - every answer recorded before `source` existed.
    """
    # Aliases that are not field names: an annotation named like a field
    # shadows it inside the other annotations' filters.
    general = Q(abstained=False, metadata__source="general")
    rows = (
        messages.annotate(day=window.day("created_at"))
        .values("day")
        .annotate(
            n_total=Count("id"),
            n_abstained=Count("id", filter=Q(abstained=True)),
            n_general=Count("id", filter=general),
            n_generative=Count("id", filter=Q(abstained=False, generative=True)),
            n_general_generative=Count("id", filter=general & Q(generative=True)),
        )
    )
    by_day = {}
    for row in rows:
        answered = row["n_total"] - row["n_abstained"] - row["n_general"]
        generative = row["n_generative"] - row["n_general_generative"]
        by_day[row["day"]] = {
            "total": row["n_total"],
            "answered": answered,
            "general": row["n_general"],
            "abstained": row["n_abstained"],
            "generative": generative,
            "quoted": answered - generative,
        }
    return by_day


def _latency(window: Window, department) -> dict:
    """p50/p95 of the time to answer, per day and over the period, and the
    median of each stage the engine timed (metadata.timings)."""
    params = {
        "role": Message.Role.ASSISTANT,
        "zone": str(window.zone),
        "start": window.start,
        "end": window.end,
    }
    join = scope = ""
    if department is not None:
        join = f"JOIN {_table(Conversation)} c ON c.id = m.conversation_id"
        scope = "AND c.department = %(department)s"
        params["department"] = department
    answers = f"FROM {_table(Message)} m {join}"
    in_period = f"""
        WHERE m.role = %(role)s
          AND m.created_at >= %(start)s AND m.created_at < %(end)s {scope}
    """
    percentiles = """
        percentile_cont(0.5) WITHIN GROUP (ORDER BY m.duration_ms) AS p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY m.duration_ms) AS p95,
        count(*) AS answers
    """
    # An answer recorded without a duration (0) was not timed, not instant.
    timed = "AND m.duration_ms > 0"
    daily = _fetch(
        f"""
        SELECT (m.created_at AT TIME ZONE %(zone)s)::date AS day, {percentiles}
        {answers} {in_period} {timed}
        GROUP BY 1
        """,
        params,
    )
    overall = _fetch(f"SELECT {percentiles} {answers} {in_period} {timed}", params)[0]
    # Each stage's milliseconds from the answer's own record. jsonb_each
    # over a non-object, or the cast of a non-number, would fail the whole
    # query; such a row is skipped instead, and an absurd value is capped
    # before the cast so it cannot overflow it.
    stages = _fetch(
        f"""
        SELECT t.key AS stage,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY LEAST(t.value::numeric, 86400000)::double precision
               ) AS median,
               count(*) AS answers
        {answers}
        CROSS JOIN LATERAL jsonb_each(
            CASE WHEN jsonb_typeof(m.metadata -> 'timings') = 'object'
                 THEN m.metadata -> 'timings' ELSE '{{}}'::jsonb END
        ) AS t(key, value)
        {in_period}
          AND jsonb_typeof(t.value) = 'number'
        GROUP BY t.key
        """,
        params,
    )

    def ms(value):
        return None if value is None else round(value)

    by_day = {
        row["day"]: {
            "p50_ms": ms(row["p50"]),
            "p95_ms": ms(row["p95"]),
            "answers": row["answers"],
        }
        for row in daily
    }
    return {
        "series": [
            {
                "date": day,
                **by_day.get(day, {"p50_ms": None, "p95_ms": None, "answers": 0}),
            }
            for day in window.dates
        ],
        "overall": {
            "p50_ms": ms(overall["p50"]),
            "p95_ms": ms(overall["p95"]),
            "answers": overall["answers"],
        },
        "stages": [
            {
                "stage": row["stage"],
                "median_ms": ms(row["median"]),
                "answers": row["answers"],
            }
            for row in sorted(
                stages, key=lambda row: _stage_order(row["stage"], ANSWER_STAGES)
            )
        ],
    }


def _abstentions(messages) -> list[dict]:
    """Why questions went unanswered, most common first.

    Each code keeps its most frequent recorded reason, so a client can
    explain it in the same words it uses for one answer.
    """
    groups: dict[str, dict] = {}
    rows = (
        messages.filter(abstained=True)
        .values("abstention_reason")
        .annotate(count=Count("id"))
        .order_by("-count", "abstention_reason")
    )
    for row in rows:
        code = abstention_code(row["abstention_reason"])
        group = groups.setdefault(
            code, {"code": code, "reason": row["abstention_reason"], "count": 0}
        )
        group["count"] += row["count"]
    return sorted(groups.values(), key=lambda group: (-group["count"], group["code"]))


# --- Feedback --------------------------------------------------------------------


def _feedback_days(window: Window, department) -> dict:
    rows = (
        scoped_feedback(department)
        .filter(created_at__gte=window.previous_start, created_at__lt=window.end)
        .annotate(day=window.day("created_at"))
        .values("day")
        .annotate(
            helpful=Count("id", filter=Q(rating=Feedback.Rating.HELPFUL)),
            not_helpful=Count("id", filter=Q(rating=Feedback.Rating.NOT_HELPFUL)),
            incorrect=Count("id", filter=Q(rating=Feedback.Rating.INCORRECT)),
        )
    )
    return {
        row["day"]: {
            "helpful": row["helpful"],
            "not_helpful": row["not_helpful"],
            "incorrect": row["incorrect"],
            "rated": row["helpful"] + row["not_helpful"] + row["incorrect"],
        }
        for row in rows
    }


def _flagged(window: Window, department) -> list[dict]:
    """The latest answers rated incorrect or not helpful in the period."""
    rows = (
        scoped_feedback(department)
        .filter(
            rating__in=FLAGS, created_at__gte=window.start, created_at__lt=window.end
        )
        .order_by("-created_at", "-id")
    )
    if department is None:
        rows = with_question(rows)
    return feedback_items(rows[:FLAGGED], department=department)


# --- Documents and processing -------------------------------------------------------


def _jobs(window: Window, department):
    """Runs in scope that finished, either way, over both periods."""
    jobs = ProcessingJob.objects.filter(
        completed_at__gte=window.previous_start,
        completed_at__lt=window.end,
        status__in=(ProcessingJob.Status.COMPLETED, ProcessingJob.Status.FAILED),
    )
    if department is not None:
        jobs = jobs.filter(document_version__document__department=department)
    return jobs


def _job_days(jobs, window: Window) -> dict:
    """Runs that finished each day, by how they finished."""
    rows = (
        jobs.annotate(day=window.day("completed_at"))
        .values("day")
        .annotate(
            completed=Count("id", filter=Q(status=ProcessingJob.Status.COMPLETED)),
            failed=Count("id", filter=Q(status=ProcessingJob.Status.FAILED)),
        )
    )
    return {
        row["day"]: {"completed": row["completed"], "failed": row["failed"]}
        for row in rows
    }


def _stage_durations(window: Window, department) -> list[dict]:
    """p50/p95 seconds of each pipeline stage that SUCCEEDED in the period.

    Skipped stages are left out: OCR skipped on a text PDF takes no time,
    and counting it would make OCR look fast exactly where it never ran.
    """
    params = {
        "succeeded": ProcessingEvent.Status.SUCCEEDED,
        "start": window.start,
        "end": window.end,
    }
    join = scope = ""
    if department is not None:
        join = f"""
            JOIN {_table(ProcessingJob)} j ON j.id = e.processing_job_id
            JOIN {_table(DocumentVersion)} v ON v.id = j.document_version_id
            JOIN {_table(Document)} d ON d.id = v.document_id
        """
        scope = "AND d.department = %(department)s"
        params["department"] = department
    seconds = (
        "GREATEST(extract(epoch FROM e.completed_at - e.started_at), 0)"
        "::double precision"
    )
    rows = _fetch(
        f"""
        SELECT e.stage,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY {seconds}) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY {seconds}) AS p95,
               count(*) AS runs
        FROM {_table(ProcessingEvent)} e {join}
        WHERE e.status = %(succeeded)s
          AND e.completed_at >= %(start)s AND e.completed_at < %(end)s
          AND e.started_at IS NOT NULL {scope}
        GROUP BY e.stage
        """,
        params,
    )
    return [
        {
            "stage": row["stage"],
            "p50_seconds": round(row["p50"], 3),
            "p95_seconds": round(row["p95"], 3),
            "runs": row["runs"],
        }
        for row in sorted(
            rows, key=lambda row: _stage_order(row["stage"], PIPELINE_STAGES)
        )
    ]


def _kind(extension: str | None) -> str:
    """The pipeline's own file type for an extension (ingestion's map)."""
    return EXTENSION_TYPES.get(f".{extension}" if extension else "", "OTHER")


def _library(window: Window, department) -> dict:
    """Uploads per day, composition, and storage."""
    versions = DocumentVersion.objects.all()
    documents = Document.objects.filter(archived_at__isnull=True)
    if department is not None:
        versions = versions.filter(document__department=department)
        documents = documents.filter(department=department)

    uploads = {
        row["day"]: {"count": row["count"], "bytes": row["bytes"] or 0}
        for row in versions.filter(
            created_at__gte=window.start, created_at__lt=window.end
        )
        .annotate(day=window.day("created_at"))
        .values("day")
        .annotate(count=Count("id"), bytes=Sum("file_size"))
    }

    totals = versions.aggregate(bytes=Sum("file_size"), files=Count("id"))
    total_bytes = totals["bytes"] or 0
    by_department = [
        {
            "department": row["department"],
            "bytes": row["bytes"] or 0,
            "files": row["files"],
        }
        for row in versions.values(department=F("document__department"))
        .annotate(bytes=Sum("file_size"), files=Count("id"))
        .order_by("-bytes", "department")
    ]
    # Grouped by the file name's extension, which upload validation checked
    # against the pipeline's list - not by mime_type, which is whatever the
    # browser declared.
    extension = Lower(
        Func(
            F("file_name"),
            Value(r"\.([^.]*)$"),
            function="substring",
            output_field=CharField(),
        )
    )
    kinds: dict[str, dict] = {}
    for row in (
        versions.annotate(extension=extension)
        .values("extension")
        .annotate(bytes=Sum("file_size"), files=Count("id"))
    ):
        name = _kind(row["extension"])
        kind = kinds.setdefault(name, {"kind": name, "bytes": 0, "files": 0})
        kind["bytes"] += row["bytes"] or 0
        kind["files"] += row["files"]

    # Storage at the end of each day of the period: what is stored now,
    # less what was uploaded after that day. Files deleted since are gone
    # from every day - this is the growth of the library as it stands.
    added_in_period = sum(day["bytes"] for day in uploads.values())
    running = total_bytes - added_in_period
    storage_series = []
    for day in window.dates:
        running += uploads.get(day, {}).get("bytes", 0)
        storage_series.append(running)

    return {
        "uploads": _series(uploads, window.dates, ("count", "bytes")),
        "by_type": [
            {"document_type": row["document_type"], "count": row["count"]}
            for row in documents.values("document_type")
            .annotate(count=Count("id"))
            .order_by("-count", "document_type")
        ],
        "by_department": [
            {"department": row["department"], "count": row["count"]}
            for row in documents.values("department")
            .annotate(count=Count("id"))
            .order_by("-count", "department")
        ],
        "storage": {
            "total_bytes": total_bytes,
            "files": totals["files"],
            "by_department": by_department,
            "by_kind": sorted(
                kinds.values(), key=lambda kind: (-kind["bytes"], kind["kind"])
            ),
        },
        "_storage_kpi": _kpi(
            total_bytes,
            total_bytes - added_in_period,
            storage_series,
            "bytes",
        ),
    }


# --- Usage -------------------------------------------------------------------------


def _activity_rows(window: Window, department):
    """Audit rows that show a member doing something.

    A failed sign-in is recorded against the account it targeted, so it is
    evidence of someone guessing, not of that person being active.
    """
    rows = AuditLog.objects.filter(
        created_at__gte=window.previous_start,
        created_at__lt=window.end,
        user__isnull=False,
    ).exclude(action=AuditLog.Action.LOGIN_FAILED)
    if department is not None:
        rows = rows.filter(user__profile__department=department)
    return rows


def _active_users(window: Window, department) -> dict:
    rows = _activity_rows(window, department)
    per_day = {
        row["day"]: {"count": row["count"]}
        for row in rows.filter(created_at__gte=window.start)
        .annotate(day=window.day("created_at"))
        .values("day")
        .annotate(count=Count("user", distinct=True))
    }
    totals = rows.aggregate(
        current=Count("user", distinct=True, filter=Q(created_at__gte=window.start)),
        previous=Count("user", distinct=True, filter=Q(created_at__lt=window.start)),
    )
    return {
        "series": _series(per_day, window.dates, ("count",)),
        "kpi": _kpi(
            totals["current"],
            totals["previous"],
            [per_day.get(day, {}).get("count", 0) for day in window.dates],
            "count",
        ),
    }


def _search_days(window: Window, department) -> dict:
    """Searches per day and how many found nothing - from the SEARCH audit
    rows, which record the number of hits and never the query."""
    searches = AuditLog.objects.filter(
        action=AuditLog.Action.SEARCH,
        created_at__gte=window.previous_start,
        created_at__lt=window.end,
    )
    if department is not None:
        searches = searches.filter(user__profile__department=department)
    return {
        row["day"]: {"count": row["count"], "zero_hit": row["zero_hit"]}
        for row in searches.annotate(day=window.day("created_at"))
        .values("day")
        .annotate(count=Count("id"), zero_hit=Count("id", filter=Q(metadata__hits=0)))
    }


def _top_cited(window: Window, department) -> list[dict]:
    """The documents answers cited most, by how many answers cited them.

    Read from each answer's recorded citations. A citation whose document
    id is not a plain integer is skipped rather than allowed to fail the
    cast mid-query.
    """
    params = {
        "role": Message.Role.ASSISTANT,
        "start": window.start,
        "end": window.end,
        "limit": TOP,
    }
    join = scope = document_scope = ""
    if department is not None:
        join = f"JOIN {_table(Conversation)} c ON c.id = m.conversation_id"
        scope = "AND c.department = %(department)s"
        document_scope = "WHERE d.department = %(department)s"
        params["department"] = department
    rows = _fetch(
        f"""
        SELECT d.id AS document_id, d.title,
               count(DISTINCT x.message_id) AS answers, count(*) AS citations
        FROM (
            SELECT m.id AS message_id,
                   CASE WHEN (elem ->> 'document_id') ~ '^[0-9]{{1,18}}$'
                        THEN (elem ->> 'document_id')::bigint END AS document_id
            FROM {_table(Message)} m {join}
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(m.citations) = 'array'
                     THEN m.citations ELSE '[]'::jsonb END
            ) AS elem
            WHERE m.role = %(role)s
              AND m.created_at >= %(start)s AND m.created_at < %(end)s {scope}
        ) x
        JOIN {_table(Document)} d ON d.id = x.document_id
        {document_scope}
        GROUP BY d.id, d.title
        ORDER BY answers DESC, citations DESC, d.id
        LIMIT %(limit)s
        """,
        params,
    )
    return rows


def _most_viewed(window: Window, department) -> list[dict]:
    """The documents opened by the most people.

    Opening one document records several views - its text, its tables, a
    cited passage - so a raw row count measures how many tabs were clicked.
    Counted instead: `readers`, distinct people, and `visits`, distinct
    person-days.
    """
    params = {
        "view": AuditLog.Action.DOCUMENT_VIEW,
        "allowed": AuditLog.Result.ALLOWED,
        "zone": str(window.zone),
        "start": window.start,
        "end": window.end,
        "limit": TOP,
    }
    document_scope = ""
    if department is not None:
        document_scope = "WHERE d.department = %(department)s"
        params["department"] = department
    return _fetch(
        f"""
        SELECT d.id AS document_id, d.title,
               count(DISTINCT v.user_id) AS readers, count(*) AS visits
        FROM (
            SELECT DISTINCT a.document_id, a.user_id,
                   (a.created_at AT TIME ZONE %(zone)s)::date AS day
            FROM {_table(AuditLog)} a
            WHERE a.action = %(view)s AND a.result = %(allowed)s
              AND a.created_at >= %(start)s AND a.created_at < %(end)s
              AND a.document_id IS NOT NULL AND a.user_id IS NOT NULL
        ) v
        JOIN {_table(Document)} d ON d.id = v.document_id
        {document_scope}
        GROUP BY d.id, d.title
        ORDER BY readers DESC, visits DESC, d.id
        LIMIT %(limit)s
        """,
        params,
    )


# --- Reports ------------------------------------------------------------------------


def build_report(key: str, department, *, now: datetime | None = None) -> dict:
    """The whole analytics report for one scope and range, uncached."""
    window = Window.ending_today(RANGES[key], now)

    messages = _answers(window, department)
    answers = _answer_days(messages, window)
    current_messages = messages.filter(created_at__gte=window.start)
    feedback = _feedback_days(window, department)
    jobs = _jobs(window, department)
    job_days = _job_days(jobs, window)
    searches = _search_days(window, department)
    active = _active_users(window, department)
    library = _library(window, department)
    storage_kpi = library.pop("_storage_kpi")

    return {
        "range": window.describe(key),
        "generated_at": timezone.now(),
        "cache_seconds": settings.ANALYTICS_CACHE_SECONDS,
        "scope": {"department": department},
        "kpis": {
            "questions": _count_kpi(answers, window, "total"),
            "answer_rate": _rate_kpi(answers, window, "answered", "total"),
            "helpful_rate": _rate_kpi(feedback, window, "helpful", "rated"),
            "documents_processed": _count_kpi(job_days, window, "completed"),
            "documents_failed": _count_kpi(job_days, window, "failed"),
            "active_users": active["kpi"],
            "searches": _count_kpi(searches, window, "count"),
            "zero_hit_rate": _rate_kpi(searches, window, "zero_hit", "count"),
            "storage_bytes": storage_kpi,
        },
        "questions": {
            "series": _series(
                answers,
                window.dates,
                ("total", "answered", "general", "abstained", "generative", "quoted"),
            ),
        },
        "latency": _latency(window, department),
        "abstentions": _abstentions(current_messages),
        "feedback": {
            "series": _series(
                feedback, window.dates, ("helpful", "not_helpful", "incorrect")
            ),
            "totals": {
                field: _total(feedback, window.dates, field)
                for field in ("helpful", "not_helpful", "incorrect")
            },
            "flagged": _flagged(window, department),
        },
        "documents": {
            "uploads": library["uploads"],
            "processing": _series(job_days, window.dates, ("completed", "failed")),
            "failures": [
                {"error_code": row["error_code"], "count": row["count"]}
                for row in jobs.filter(
                    status=ProcessingJob.Status.FAILED,
                    completed_at__gte=window.start,
                )
                .values("error_code")
                .annotate(count=Count("id"))
                .order_by("-count", "error_code")
            ],
            "stages": _stage_durations(window, department),
            "by_type": library["by_type"],
            "by_department": library["by_department"],
            "storage": library["storage"],
        },
        "usage": {
            "active_users": active["series"],
            "searches": _series(searches, window.dates, ("count", "zero_hit")),
            "top_cited": _top_cited(window, department),
            "most_viewed": _most_viewed(window, department),
        },
    }


def _scope_key(department) -> str:
    # Hashed: a department name is free text, and a cache key must not
    # carry spaces or run past a backend's key length.
    if department is None:
        return "all"
    return "dept-" + hashlib.sha256(department.encode()).hexdigest()[:24]


def _cache_key(kind: str, *parts: str) -> str:
    return ":".join(
        ("analytics", f"v{SCHEMA}", kind, *parts, settings.ANALYTICS_TIME_ZONE)
    )


def report(key: str, department, *, fresh: bool = False) -> dict:
    """The cached report; `fresh` computes it again and replaces the copy."""
    cache_key = _cache_key("report", key, _scope_key(department))
    if not fresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    data = build_report(key, department)
    cache.set(cache_key, data, settings.ANALYTICS_CACHE_SECONDS)
    return data


def wants_fresh(request) -> bool:
    return request.query_params.get("fresh", "").strip().lower() in ("1", "true", "yes")


class AnalyticsView(APIView):
    """GET /api/v1/admin/analytics/ - see the module docstring."""

    permission_classes = [IsAdministrator]
    throttle_classes = [UserRateThrottle, BurstUserThrottle]

    def get_throttles(self):
        # Only a forced recomputation carries the refresh budget; a cached
        # report costs a cache read.
        self.throttle_scope = "analytics_refresh" if wants_fresh(self.request) else None
        return super().get_throttles()

    def get(self, request):
        key = request.query_params.get("range", "").strip() or DEFAULT_RANGE
        if key not in RANGES:
            raise exceptions.ValidationError(
                {"range": [f"Must be one of {', '.join(RANGES)}."]}
            )
        return Response(
            report(key, department_scope(request.user), fresh=wants_fresh(request))
        )


# --- The dashboard -------------------------------------------------------------------


def org_overview(department) -> dict:
    """The dashboard's "this week" strip for an administrator: four of the
    report's headline numbers over the last seven days, cached briefly."""
    cache_key = _cache_key("org", _scope_key(department))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    window = Window.ending_today(ORG_DAYS)
    answers = _answer_days(_answers(window, department), window)
    feedback = _feedback_days(window, department)
    job_days = _job_days(_jobs(window, department), window)
    data = {
        "range": window.describe(f"{ORG_DAYS}d"),
        "generated_at": timezone.now(),
        "scope": {"department": department},
        "kpis": {
            "questions": _count_kpi(answers, window, "total"),
            "answer_rate": _rate_kpi(answers, window, "answered", "total"),
            "helpful_rate": _rate_kpi(feedback, window, "helpful", "rated"),
            "documents_processed": _count_kpi(job_days, window, "completed"),
        },
    }
    cache.set(cache_key, data, settings.DASHBOARD_ACTIVITY_CACHE_SECONDS)
    return data


ACTIVITY_WEEKS = 12
ACTIVITY_DAYS = 30


def _personal_activity(user) -> dict:
    """What the reader can see and what they asked, for their home screen.

    Uploads and composition are over `visible_documents` - exactly the
    documents the reader may open, as everything else on the dashboard is.
    Questions are the reader's own, from their own conversations.
    """
    zone = analytics_zone()
    today = timezone.localdate(timezone.now(), zone)
    visible = authorization.visible_documents(
        user, Document.objects.filter(archived_at__isnull=True)
    )

    this_week = today - timedelta(days=today.weekday())
    weeks = [
        this_week - timedelta(weeks=ACTIVITY_WEEKS - 1 - n)
        for n in range(ACTIVITY_WEEKS)
    ]
    uploads = {
        _as_date(row["week"], zone): row["count"]
        for row in DocumentVersion.objects.filter(
            document__in=visible,
            created_at__gte=datetime.combine(weeks[0], time.min, tzinfo=zone),
        )
        .annotate(week=TruncWeek("created_at", tzinfo=zone))
        .values("week")
        .annotate(count=Count("id"))
    }

    window = Window(ACTIVITY_DAYS, zone, today)
    questions = _answer_days(
        Message.objects.filter(
            conversation__owner=user,
            role=Message.Role.ASSISTANT,
            created_at__gte=window.start,
            created_at__lt=window.end,
        ),
        window,
    )

    return {
        "generated_at": timezone.now(),
        "time_zone": str(zone),
        "uploads_by_week": [
            {"week": week, "count": uploads.get(week, 0)} for week in weeks
        ],
        "questions_by_day": [
            {
                "date": day,
                "count": questions.get(day, {}).get("total", 0),
                "answered": questions.get(day, {}).get("answered", 0),
            }
            for day in window.dates
        ],
        "documents_by_type": [
            {"document_type": row["document_type"], "count": row["count"]}
            for row in visible.values("document_type")
            .annotate(count=Count("id"))
            .order_by("-count", "document_type")
        ],
        "documents_by_department": [
            {"department": row["department"], "count": row["count"]}
            for row in visible.values("department")
            .annotate(count=Count("id"))
            .order_by("-count", "department")
        ],
    }


def dashboard_activity(user) -> dict:
    """DashboardView's `activity` block (see docs/api/analytics.md).

    The personal part is cached per user, the `org` part per scope; both
    for DASHBOARD_ACTIVITY_CACHE_SECONDS, because the dashboard polls while
    documents are processing.
    """
    cache_key = _cache_key("activity", f"user-{user.pk}")
    activity = cache.get(cache_key)
    if activity is None:
        activity = _personal_activity(user)
        cache.set(cache_key, activity, settings.DASHBOARD_ACTIVITY_CACHE_SECONDS)
    return {
        **activity,
        # The same rule as the view_operations capability: administrators.
        "org": org_overview(department_scope(user)) if is_administrator(user) else None,
    }
