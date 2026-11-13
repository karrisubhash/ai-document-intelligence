"""The analytics read model, the feedback review queue, and the dashboard's
activity block (documents.operations.analytics / .feedback)."""

import json
from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib import admin
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from accounts.models import UserProfile
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
from documents.operations import analytics
from documents.rag.engine import Abstention
from documents.tests import make_user

Role = UserProfile.Role
URL = "/api/v1/admin/analytics/"
FEEDBACK_URL = "/api/v1/admin/feedback/"


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def at_noon(days_ago=0):
    """Noon, `days_ago` days before today, in the analytics time zone: far
    from either edge of its day bucket."""
    zone = analytics.analytics_zone()
    day = timezone.localdate(timezone.now(), zone) - timedelta(days=days_ago)
    return datetime.combine(day, time(12), tzinfo=zone)


def day_of(days_ago=0):
    return at_noon(days_ago).date().isoformat()


def ask(
    owner,
    *,
    department=None,
    days_ago=0,
    question="What is the attendance rule?",
    content="Attendance is 75 percent. [1]",
    abstained=False,
    reason="",
    source="documents",
    generative=False,
    duration_ms=1200,
    timings=None,
    citations=None,
    metadata=None,
):
    """One question and its answer, as ChatView records them."""
    conversation = Conversation.objects.create(
        owner=owner,
        department=owner.profile.department if department is None else department,
    )
    Message.objects.create(
        conversation=conversation, role=Message.Role.USER, content=question
    )
    answer = Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=content,
        abstained=abstained,
        abstention_reason=reason,
        citations=citations or [],
        generative=generative,
        duration_ms=duration_ms,
        model_version="gemini/flash" if generative else "",
        model_name="extractive-answerer" if not generative else "gemini",
        metadata=metadata
        if metadata is not None
        else {"source": source, "timings": timings or {}},
    )
    Message.objects.filter(conversation=conversation).update(
        created_at=at_noon(days_ago)
    )
    answer.refresh_from_db()
    return answer


def rate(answer, rating, *, days_ago=0, comment=""):
    feedback = Feedback.objects.create(
        message=answer,
        user=answer.conversation.owner,
        rating=rating,
        comment=comment,
        answer_abstained=answer.abstained,
        answer_citations=len(answer.citations),
    )
    Feedback.objects.filter(pk=feedback.pk).update(created_at=at_noon(days_ago))
    return feedback


def document(
    owner,
    title="Policy",
    *,
    department=None,
    file_name="policy.pdf",
    size=1000,
    days_ago=0,
    document_type=Document.DocumentType.POLICY,
):
    doc = Document.objects.create(
        title=title,
        owner=owner,
        department=owner.profile.department if department is None else department,
        document_type=document_type,
    )
    version = DocumentVersion.objects.create(
        document=doc,
        version_number=1,
        file=f"documents/{doc.id}/versions/1/{file_name}",
        file_name=file_name,
        file_size=size,
        uploaded_by=owner,
    )
    DocumentVersion.objects.filter(pk=version.pk).update(created_at=at_noon(days_ago))
    doc.current_version = version
    doc.save(update_fields=["current_version"])
    return doc


def run(doc, status_, *, days_ago=0, error_code="", stages=()):
    job = ProcessingJob.objects.create(
        document_version=doc.current_version,
        status=status_,
        error_code=error_code,
        completed_at=at_noon(days_ago),
    )
    for stage, seconds in stages:
        started = at_noon(days_ago)
        ProcessingEvent.objects.create(
            processing_job=job,
            stage=stage,
            status=ProcessingEvent.Status.SUCCEEDED,
            started_at=started,
            completed_at=started + timedelta(seconds=seconds),
        )
    return job


def audit(
    user,
    action,
    *,
    days_ago=0,
    result=AuditLog.Result.ALLOWED,
    document=None,
    **metadata,
):
    row = AuditLog.objects.create(
        user=user, action=action, result=result, document=document, metadata=metadata
    )
    AuditLog.objects.filter(pk=row.pk).update(created_at=at_noon(days_ago))
    return row


class AnalyticsTestCase(TestCase):
    def setUp(self):
        # The report is cached across requests - and the test cache across
        # tests - so every test starts from nothing.
        cache.clear()
        self.addCleanup(cache.clear)
        self.root = make_user("an_root", Role.SYSTEM_ADMIN, "")
        self.physics_admin = make_user("an_phy_admin", Role.DEPARTMENT_ADMIN, "Physics")
        self.physics = make_user("an_phy", Role.STUDENT, "Physics")
        self.chemistry = make_user("an_che", Role.STUDENT, "Chemistry")

    def report(self, user, **params):
        response = client_for(user).get(URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()


class AnalyticsAccessTests(AnalyticsTestCase):
    def test_only_administrators_may_read_it(self):
        self.assertIn(
            APIClient().get(URL).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        response = client_for(self.physics).get(URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        for user in (self.physics_admin, self.root):
            self.assertEqual(client_for(user).get(URL).status_code, status.HTTP_200_OK)

    def test_both_prefixes_serve_it(self):
        self.assertEqual(
            client_for(self.root).get("/api/admin/analytics/").status_code,
            status.HTTP_200_OK,
        )

    def test_an_unknown_range_is_a_400(self):
        response = client_for(self.root).get(URL, {"range": "1y"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("range", response.json())


class EmptyInstallationTests(AnalyticsTestCase):
    def test_every_series_has_every_day_and_nothing_is_invented(self):
        for key, days in analytics.RANGES.items():
            with self.subTest(range=key):
                data = self.report(self.root, range=key)
                self.assertEqual(data["range"]["days"], days)
                self.assertEqual(data["range"]["end"], day_of(0))
                self.assertEqual(data["range"]["start"], day_of(days - 1))
                self.assertEqual(data["scope"], {"department": None})
                self.assertIn("generated_at", data)
                series = [
                    data["questions"]["series"],
                    data["latency"]["series"],
                    data["feedback"]["series"],
                    data["documents"]["uploads"],
                    data["documents"]["processing"],
                    data["usage"]["active_users"],
                    data["usage"]["searches"],
                ] + [kpi["series"] for kpi in data["kpis"].values()]
                for points in series:
                    self.assertEqual(len(points), days)
                dates = [point["date"] for point in data["questions"]["series"]]
                self.assertEqual(dates, sorted(dates))
                self.assertEqual(dates[-1], day_of(0))

                kpis = data["kpis"]
                self.assertEqual(kpis["questions"]["value"], 0)
                self.assertEqual(kpis["questions"]["delta"], 0)
                # A rate with nothing to measure is unknown, not zero.
                self.assertIsNone(kpis["answer_rate"]["value"])
                self.assertIsNone(kpis["answer_rate"]["delta"])
                self.assertIsNone(kpis["helpful_rate"]["value"])
                self.assertEqual(kpis["storage_bytes"]["value"], 0)
                self.assertEqual(data["latency"]["series"][0]["p50_ms"], None)
                self.assertEqual(data["abstentions"], [])
                self.assertEqual(data["feedback"]["flagged"], [])
                self.assertEqual(data["usage"]["top_cited"], [])


class AnswerAnalyticsTests(AnalyticsTestCase):
    def test_questions_are_split_by_how_they_were_answered(self):
        ask(self.physics, generative=True, days_ago=1)
        ask(self.physics, generative=False, days_ago=1)
        ask(
            self.physics,
            source="general",
            generative=True,
            content="Hello!",
            days_ago=1,
        )
        ask(
            self.physics,
            abstained=True,
            reason=Abstention.NO_RELEVANT_PASSAGES,
            source="none",
            days_ago=1,
        )
        # Recorded before `source` existed: answered, from the documents.
        ask(self.physics, metadata={}, days_ago=1)

        data = self.report(self.root, range="7d")
        day = next(p for p in data["questions"]["series"] if p["date"] == day_of(1))
        self.assertEqual(
            day,
            {
                "date": day_of(1),
                "total": 5,
                "answered": 3,
                "general": 1,
                "abstained": 1,
                "generative": 1,
                "quoted": 2,
            },
        )
        self.assertEqual(data["kpis"]["questions"]["value"], 5)
        self.assertEqual(data["kpis"]["answer_rate"]["value"], 0.6)

    def test_the_previous_period_is_the_comparison(self):
        for _ in range(3):
            ask(self.physics, days_ago=9)  # previous 7 days
        ask(self.physics, days_ago=2)
        ask(self.physics, days_ago=2, abstained=True, reason=Abstention.NO_DOCUMENTS)

        kpis = self.report(self.root, range="7d")["kpis"]
        self.assertEqual(kpis["questions"]["value"], 2)
        self.assertEqual(kpis["questions"]["previous"], 3)
        self.assertEqual(kpis["questions"]["delta"], -1)
        self.assertEqual(kpis["answer_rate"]["value"], 0.5)
        self.assertEqual(kpis["answer_rate"]["previous"], 1.0)
        self.assertEqual(kpis["answer_rate"]["delta"], -0.5)
        self.assertEqual(kpis["questions"]["series"][-3], 2)

    def test_latency_percentiles_and_stage_medians(self):
        for duration in (100, 200, 300, 400, 1000):
            ask(
                self.physics,
                duration_ms=duration,
                timings={"retrieval": duration // 10, "rerank": 50, "generation": 7},
            )
        # Untimed (0 is "not measured", not "instant"), and timings the
        # engine never writes: skipped, not fatal.
        ask(self.physics, duration_ms=0, timings={"retrieval": "slow"})
        ask(self.physics, duration_ms=0, metadata={"timings": [1, 2]})

        latency = self.report(self.root, range="7d")["latency"]
        today = latency["series"][-1]
        self.assertEqual(today["answers"], 5)
        self.assertEqual(today["p50_ms"], 300)
        # Interpolated, as percentile_cont does: 400 + 0.8 * (1000 - 400).
        self.assertEqual(today["p95_ms"], 880)
        self.assertEqual(
            latency["overall"], {"p50_ms": 300, "p95_ms": 880, "answers": 5}
        )
        self.assertEqual(
            latency["stages"],
            [
                {"stage": "retrieval", "median_ms": 30, "answers": 5},
                {"stage": "rerank", "median_ms": 50, "answers": 5},
                {"stage": "generation", "median_ms": 7, "answers": 5},
            ],
        )
        self.assertIsNone(latency["series"][0]["p50_ms"])

    def test_abstentions_are_grouped_by_reason(self):
        for _ in range(2):
            ask(self.physics, abstained=True, reason=Abstention.NO_RELEVANT_PASSAGES)
        ask(
            self.physics,
            abstained=True,
            reason="none of the 2 parts of the question could be answered from the authorized documents",
        )
        ask(
            self.physics,
            abstained=True,
            reason="none of the 3 parts of the question could be answered from the authorized documents",
        )
        ask(self.physics, abstained=True, reason=Abstention.UNGROUNDED)

        abstentions = self.report(self.root, range="7d")["abstentions"]
        self.assertEqual(
            [(row["code"], row["count"]) for row in abstentions],
            [("not_relevant", 2), ("parts_unanswered", 2), ("ungrounded", 1)],
        )
        self.assertEqual(abstentions[0]["reason"], Abstention.NO_RELEVANT_PASSAGES)

    def test_a_department_administrator_sees_their_departments_answers(self):
        ask(self.physics)
        ask(self.chemistry)
        ask(self.chemistry)

        self.assertEqual(
            self.report(self.physics_admin, range="7d")["kpis"]["questions"]["value"], 1
        )
        self.assertEqual(
            self.report(self.root, range="7d")["kpis"]["questions"]["value"], 3
        )

    def test_the_day_is_the_analytics_time_zones(self):
        conversation = Conversation.objects.create(
            owner=self.physics, department="Physics"
        )
        answer = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT, content="x"
        )
        zone = "Asia/Kolkata"
        with override_settings(ANALYTICS_TIME_ZONE=zone):
            # 20:00 UTC yesterday is 01:30 today in India.
            utc_today = timezone.localdate(timezone.now(), UTC)
            late = datetime.combine(utc_today - timedelta(days=1), time(20), tzinfo=UTC)
            Message.objects.filter(pk=answer.pk).update(created_at=late)
            data = self.report(self.root, range="7d")
            local = late.astimezone(analytics.analytics_zone()).date().isoformat()
            self.assertEqual(data["range"]["time_zone"], zone)
            point = next(p for p in data["questions"]["series"] if p["date"] == local)
            self.assertEqual(point["total"], 1)
            self.assertEqual(local, utc_today.isoformat())


class FeedbackAnalyticsTests(AnalyticsTestCase):
    def test_feedback_per_day_and_the_helpful_rate(self):
        rate(ask(self.physics), Feedback.Rating.HELPFUL)
        rate(ask(self.physics), Feedback.Rating.HELPFUL)
        rate(ask(self.physics), Feedback.Rating.INCORRECT)
        rate(ask(self.physics, days_ago=8), Feedback.Rating.NOT_HELPFUL, days_ago=8)

        data = self.report(self.root, range="7d")
        self.assertEqual(
            data["feedback"]["series"][-1],
            {"date": day_of(0), "helpful": 2, "not_helpful": 0, "incorrect": 1},
        )
        self.assertEqual(
            data["feedback"]["totals"], {"helpful": 2, "not_helpful": 0, "incorrect": 1}
        )
        helpful = data["kpis"]["helpful_rate"]
        self.assertEqual(helpful["value"], round(2 / 3, 4))
        self.assertEqual(helpful["previous"], 0.0)

    def test_flagged_answers_carry_the_question_only_for_a_system_admin(self):
        answer = ask(self.physics, question="Is the hostel curfew at 10?")
        rate(answer, Feedback.Rating.INCORRECT, comment="It is 11.")
        rate(ask(self.physics, question="Thanks!"), Feedback.Rating.HELPFUL)

        flagged = self.report(self.root, range="7d")["feedback"]["flagged"]
        self.assertEqual(len(flagged), 1)
        item = flagged[0]
        self.assertEqual(item["question"], "Is the hostel curfew at 10?")
        self.assertEqual(item["comment"], "It is 11.")
        self.assertEqual(item["rating"], "INCORRECT")
        self.assertEqual(item["answer_excerpt"], "Attendance is 75 percent. [1]")
        self.assertNotIn("user", item)

        department_view = self.report(self.physics_admin, range="7d")
        self.assertIsNone(department_view["feedback"]["flagged"][0]["question"])
        self.assertNotIn("hostel curfew", json.dumps(department_view))

    def test_a_flagged_answer_is_dated_to_the_day_only(self):
        # Built like a review-queue item, so dated like one: nothing to
        # match against the audit log's CHAT rows (see FeedbackQueueTests).
        rate(ask(self.physics), Feedback.Rating.INCORRECT)

        for user in (self.root, self.physics_admin):
            with self.subTest(user=user.username):
                item = self.report(user, range="7d")["feedback"]["flagged"][0]
                self.assertEqual(item["rated_on"], day_of(0))
                for field in ("created_at", "answered_at", "message_id"):
                    self.assertNotIn(field, item)


class DocumentAnalyticsTests(AnalyticsTestCase):
    def test_uploads_runs_failures_and_stage_durations(self):
        lab = document(self.physics, "Lab rules", days_ago=1, size=2048)
        scan = document(self.physics, "Old scan", file_name="scan.PNG", size=4096)
        notes = document(self.chemistry, "Notes", file_name="notes.txt", size=10)
        run(
            lab,
            ProcessingJob.Status.COMPLETED,
            days_ago=1,
            stages=[("OCR", 4), ("INDEXING", 1)],
        )
        run(lab, ProcessingJob.Status.COMPLETED, stages=[("OCR", 2)])
        run(scan, ProcessingJob.Status.FAILED, error_code="OCR_FAILED")
        run(notes, ProcessingJob.Status.FAILED, error_code="EMPTY_EXTRACTION")
        # Still running: not finished, so not counted either way.
        ProcessingJob.objects.create(
            document_version=notes.current_version,
            status=ProcessingJob.Status.PROCESSING,
        )

        data = self.report(self.root, range="7d")
        documents = data["documents"]
        self.assertEqual(
            documents["uploads"][-2], {"date": day_of(1), "count": 1, "bytes": 2048}
        )
        self.assertEqual(documents["uploads"][-1]["count"], 2)
        self.assertEqual(
            documents["processing"][-1],
            {"date": day_of(0), "completed": 1, "failed": 2},
        )
        self.assertEqual(data["kpis"]["documents_processed"]["value"], 2)
        self.assertEqual(data["kpis"]["documents_failed"]["value"], 2)
        self.assertEqual(
            documents["failures"],
            [
                {"error_code": "EMPTY_EXTRACTION", "count": 1},
                {"error_code": "OCR_FAILED", "count": 1},
            ],
        )
        stages = {row["stage"]: row for row in documents["stages"]}
        self.assertEqual(stages["OCR"]["runs"], 2)
        self.assertEqual(stages["OCR"]["p50_seconds"], 3.0)
        self.assertEqual(stages["INDEXING"]["p95_seconds"], 1.0)
        self.assertLess(
            [row["stage"] for row in documents["stages"]].index("OCR"),
            [row["stage"] for row in documents["stages"]].index("INDEXING"),
        )

        storage = documents["storage"]
        self.assertEqual(storage["total_bytes"], 2048 + 4096 + 10)
        self.assertEqual(
            {row["kind"]: row["bytes"] for row in storage["by_kind"]},
            {"IMAGE": 4096, "PDF": 2048, "TXT": 10},
        )
        self.assertEqual(
            {row["department"]: row["files"] for row in storage["by_department"]},
            {"Physics": 2, "Chemistry": 1},
        )
        storage_kpi = data["kpis"]["storage_bytes"]
        self.assertEqual(storage_kpi["value"], 6154)
        self.assertEqual(storage_kpi["previous"], 0)
        self.assertEqual(storage_kpi["series"][-2], 2048)
        self.assertEqual(storage_kpi["series"][-1], 6154)

    def test_a_department_administrator_sees_their_departments_documents(self):
        lab = document(self.physics, "Lab rules")
        notes = document(
            self.chemistry, "Notes", document_type=Document.DocumentType.FORM
        )
        run(lab, ProcessingJob.Status.COMPLETED, stages=[("OCR", 1)])
        run(
            notes,
            ProcessingJob.Status.FAILED,
            error_code="OCR_FAILED",
            stages=[("OCR", 9)],
        )

        documents = self.report(self.physics_admin, range="7d")["documents"]
        self.assertEqual(documents["failures"], [])
        self.assertEqual(documents["stages"][0]["p95_seconds"], 1.0)
        self.assertEqual(
            documents["by_type"], [{"document_type": "POLICY", "count": 1}]
        )
        self.assertEqual(
            documents["by_department"], [{"department": "Physics", "count": 1}]
        )
        self.assertEqual(documents["storage"]["files"], 1)


class UsageAnalyticsTests(AnalyticsTestCase):
    def test_active_users_leave_out_failed_sign_ins(self):
        audit(self.physics, AuditLog.Action.LOGIN)
        audit(self.physics, AuditLog.Action.SEARCH, hits=3)
        audit(
            self.chemistry, AuditLog.Action.LOGIN_FAILED, result=AuditLog.Result.DENIED
        )
        audit(self.chemistry, AuditLog.Action.LOGIN, days_ago=9)

        data = self.report(self.root, range="7d")
        self.assertEqual(
            data["usage"]["active_users"][-1], {"date": day_of(0), "count": 1}
        )
        active = data["kpis"]["active_users"]
        self.assertEqual((active["value"], active["previous"]), (1, 1))

    def test_searches_and_the_zero_hit_rate(self):
        audit(self.physics, AuditLog.Action.SEARCH, hits=0, authorized_documents=4)
        audit(self.physics, AuditLog.Action.SEARCH, hits=5, authorized_documents=4)
        audit(self.physics, AuditLog.Action.SEARCH, hits=0, authorized_documents=4)
        audit(self.chemistry, AuditLog.Action.SEARCH, hits=2, authorized_documents=1)

        data = self.report(self.root, range="7d")
        self.assertEqual(
            data["usage"]["searches"][-1],
            {"date": day_of(0), "count": 4, "zero_hit": 2},
        )
        self.assertEqual(data["kpis"]["zero_hit_rate"]["value"], 0.5)
        physics = self.report(self.physics_admin, range="7d")
        self.assertEqual(physics["kpis"]["searches"]["value"], 3)

    def test_most_cited_documents_name_only_what_the_scope_may_see(self):
        lab = document(self.physics, "Lab rules")
        salary = document(self.chemistry, "Salary revisions")
        cite = [
            {"document_id": lab.id, "marker": 1},
            {"document_id": lab.id, "marker": 2},
        ]
        ask(self.physics, citations=cite)
        ask(self.physics, citations=[{"document_id": lab.id, "marker": 1}])
        # A physics member's answer that cites a chemistry document (their
        # own, uploaded elsewhere), and citations no code would write.
        ask(self.physics, citations=[{"document_id": salary.id, "marker": 1}])
        ask(self.physics, citations=[{"document_id": "x1"}, {"marker": 1}, "junk"])
        ask(self.physics, citations={"not": "a list"})

        root = self.report(self.root, range="7d")["usage"]["top_cited"]
        self.assertEqual(
            root,
            [
                {
                    "document_id": lab.id,
                    "title": "Lab rules",
                    "answers": 2,
                    "citations": 3,
                },
                {
                    "document_id": salary.id,
                    "title": "Salary revisions",
                    "answers": 1,
                    "citations": 1,
                },
            ],
        )
        physics = self.report(self.physics_admin, range="7d")
        self.assertEqual(
            [row["title"] for row in physics["usage"]["top_cited"]], ["Lab rules"]
        )
        self.assertNotIn("Salary revisions", json.dumps(physics))

    def test_most_viewed_counts_readers_and_visits_not_rows(self):
        lab = document(self.physics, "Lab rules")
        salary = document(self.chemistry, "Salary revisions")
        # Opening a document records several views (text, tables, a source).
        for _ in range(4):
            audit(self.physics, AuditLog.Action.DOCUMENT_VIEW, document=lab)
        audit(self.physics, AuditLog.Action.DOCUMENT_VIEW, document=lab, days_ago=1)
        audit(self.physics_admin, AuditLog.Action.DOCUMENT_VIEW, document=lab)
        audit(self.chemistry, AuditLog.Action.DOCUMENT_VIEW, document=salary)
        # A refusal is not a read.
        for _ in range(5):
            audit(
                self.physics,
                AuditLog.Action.DOCUMENT_VIEW,
                document=salary,
                result=AuditLog.Result.DENIED,
            )

        viewed = self.report(self.root, range="7d")["usage"]["most_viewed"]
        self.assertEqual(
            viewed,
            [
                {
                    "document_id": lab.id,
                    "title": "Lab rules",
                    "readers": 2,
                    "visits": 3,
                },
                {
                    "document_id": salary.id,
                    "title": "Salary revisions",
                    "readers": 1,
                    "visits": 1,
                },
            ],
        )
        physics = self.report(self.physics_admin, range="7d")["usage"]["most_viewed"]
        self.assertEqual([row["title"] for row in physics], ["Lab rules"])


class AnalyticsCachingTests(AnalyticsTestCase):
    def test_a_report_is_cached_per_scope_and_range_until_refreshed(self):
        ask(self.physics)
        first = self.report(self.physics_admin, range="7d")
        ask(self.physics)
        self.assertEqual(self.report(self.physics_admin, range="7d"), first)
        # Another scope, or another range, is its own report.
        self.assertEqual(
            self.report(self.root, range="7d")["kpis"]["questions"]["value"], 2
        )
        self.assertEqual(
            self.report(self.physics_admin, range="30d")["kpis"]["questions"]["value"],
            2,
        )
        fresh = self.report(self.physics_admin, range="7d", fresh="1")
        self.assertEqual(fresh["kpis"]["questions"]["value"], 2)
        self.assertEqual(self.report(self.physics_admin, range="7d"), fresh)

    def test_a_cached_report_costs_no_aggregation(self):
        self.report(self.root, range="30d")
        with CaptureQueriesContext(connection) as queries:
            self.report(self.root, range="30d")
        aggregations = [q for q in queries.captured_queries if "GROUP BY" in q["sql"]]
        self.assertEqual(aggregations, [])

    def test_refreshing_is_throttled_and_reading_is_not(self):
        client = client_for(self.root)
        # SimpleRateThrottle binds THROTTLE_RATES at import time.
        with patch.dict(
            SimpleRateThrottle.THROTTLE_RATES, {"analytics_refresh": "2/hour"}
        ):
            statuses = [client.get(URL, {"fresh": "1"}).status_code for _ in range(3)]
            self.assertEqual(statuses, [200, 200, 429])
            self.assertEqual(client.get(URL).status_code, status.HTTP_200_OK)


class FeedbackQueueTests(AnalyticsTestCase):
    def setUp(self):
        super().setUp()
        self.lab = document(self.physics, "Lab rules")
        self.salary = document(self.chemistry, "Salary revisions")
        self.wrong = rate(
            ask(
                self.physics,
                question="When does the lab close?",
                content="The lab closes at 6 pm. [1]",
                citations=[{"document_id": self.lab.id, "marker": 1}],
            ),
            Feedback.Rating.INCORRECT,
            comment="It closes at 8.",
        )
        # A physics member's answer quoting a chemistry document.
        self.elsewhere = rate(
            ask(
                self.physics,
                question="What are the new salaries?",
                content="Salaries rise by 4 percent. [1]",
                citations=[{"document_id": self.salary.id, "marker": 1}],
            ),
            Feedback.Rating.NOT_HELPFUL,
            days_ago=2,
        )
        self.good = rate(
            ask(self.physics, question="Thanks", content="You are welcome."),
            Feedback.Rating.HELPFUL,
            days_ago=1,
        )
        self.chemistry_flag = rate(
            ask(self.chemistry, question="Chemistry question"),
            Feedback.Rating.INCORRECT,
        )

    def items(self, user, **params):
        response = client_for(user).get(FEEDBACK_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()["results"]

    def test_only_administrators_may_read_it(self):
        self.assertEqual(
            client_for(self.physics).get(FEEDBACK_URL).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_newest_first_and_scoped_to_the_department(self):
        self.assertEqual(
            [item["id"] for item in self.items(self.physics_admin)],
            [self.wrong.id, self.good.id, self.elsewhere.id],
        )
        self.assertEqual(len(self.items(self.root)), 4)

    def test_filters(self):
        self.assertEqual(
            [item["id"] for item in self.items(self.root, rating="INCORRECT")],
            sorted([self.wrong.id, self.chemistry_flag.id], reverse=True),
        )
        self.assertEqual(
            {
                item["id"]
                for item in self.items(
                    self.physics_admin, rating="incorrect,NOT_HELPFUL"
                )
            },
            {self.wrong.id, self.elsewhere.id},
        )
        self.assertEqual(
            [item["id"] for item in self.items(self.physics_admin, since=day_of(1))],
            [self.wrong.id, self.good.id],
        )
        for params in ({"rating": "MEH"}, {"since": "yesterday"}):
            with self.subTest(params=params):
                response = client_for(self.root).get(FEEDBACK_URL, params)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_what_a_department_administrator_is_shown(self):
        items = {item["id"]: item for item in self.items(self.physics_admin)}
        wrong = items[self.wrong.id]
        self.assertIsNone(wrong["question"])
        self.assertEqual(wrong["answer_excerpt"], "The lab closes at 6 pm. [1]")
        self.assertFalse(wrong["answer_withheld"])
        self.assertEqual(
            wrong["documents"], [{"id": self.lab.id, "title": "Lab rules"}]
        )
        self.assertEqual(wrong["comment"], "It closes at 8.")
        self.assertEqual(wrong["citations"], 1)
        # It quotes another department's document: neither the excerpt nor
        # the document is shown.
        elsewhere = items[self.elsewhere.id]
        self.assertTrue(elsewhere["answer_withheld"])
        self.assertEqual(elsewhere["answer_excerpt"], "")
        self.assertEqual(elsewhere["documents"], [])
        body = json.dumps(self.items(self.physics_admin))
        for secret in ("Salaries rise", "Salary revisions", "lab close?", "an_phy"):
            self.assertNotIn(secret, body)

    def test_what_a_system_administrator_is_shown(self):
        items = {item["id"]: item for item in self.items(self.root)}
        self.assertEqual(items[self.wrong.id]["question"], "When does the lab close?")
        self.assertEqual(
            items[self.elsewhere.id]["question"], "What are the new salaries?"
        )
        self.assertFalse(items[self.elsewhere.id]["answer_withheld"])
        # A HELPFUL rating asked nobody to look at the question.
        self.assertIsNone(items[self.good.id]["question"])

    def test_a_long_answer_is_cut_to_an_excerpt(self):
        text = "word " * 200
        feedback = rate(ask(self.physics, content=text), Feedback.Rating.INCORRECT)
        item = next(i for i in self.items(self.physics_admin) if i["id"] == feedback.id)
        self.assertLessEqual(len(item["answer_excerpt"]), 281)
        self.assertTrue(item["answer_excerpt"].endswith("…"))

    def test_nothing_dates_a_rating_closer_than_its_day(self):
        # ChatView writes a CHAT audit row under the asker's name within
        # milliseconds of every answer, an administrator reads those rows,
        # and only the asker can rate the answer: an exact answer or rating
        # time here named the rater. The answer's id is a clock as well -
        # ids are handed out in order, and the reader's own messages say when.
        exact = at_noon(0) + timedelta(seconds=7, microseconds=512331)
        Message.objects.filter(pk=self.wrong.message_id).update(created_at=exact)
        Feedback.objects.filter(pk=self.wrong.pk).update(
            created_at=exact + timedelta(seconds=4)
        )

        for user in (self.physics_admin, self.root):
            with self.subTest(user=user.username):
                items = self.items(user)
                item = next(i for i in items if i["id"] == self.wrong.id)
                self.assertEqual(item["rated_on"], day_of(0))
                for field in ("created_at", "answered_at", "message_id"):
                    self.assertNotIn(field, item)
                # No time of day anywhere on the page.
                self.assertNotRegex(json.dumps(items), r"\d\d:\d\d")

    def test_since_names_a_day_not_an_instant(self):
        # A bound to the second, asked again a little later each time, would
        # tell to the second when an answer was rated: the time the items
        # no longer carry.
        just_after = (at_noon(0) + timedelta(seconds=1)).isoformat()
        self.assertIn(
            self.wrong.id,
            [item["id"] for item in self.items(self.physics_admin, since=just_after)],
        )
        late_yesterday = (at_noon(1) + timedelta(hours=11)).isoformat()
        self.assertEqual(
            [
                item["id"]
                for item in self.items(self.physics_admin, since=late_yesterday)
            ],
            [self.wrong.id, self.good.id],
        )
        # An instant with no day in the analytics time zone is refused, not
        # a 500.
        response = client_for(self.root).get(
            FEEDBACK_URL, {"since": "0001-01-01T00:00:00+05:00"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_day_is_the_analytics_time_zones(self):
        zone = ZoneInfo("Asia/Kolkata")
        with override_settings(ANALYTICS_TIME_ZONE="Asia/Kolkata"):
            # 02:00 in India is 20:30 the day before in UTC.
            day = timezone.localdate(timezone.now(), zone)
            Feedback.objects.filter(pk=self.wrong.pk).update(
                created_at=datetime.combine(day, time(2), tzinfo=zone)
            )
            item = next(i for i in self.items(self.root) if i["id"] == self.wrong.id)
            self.assertEqual(item["rated_on"], day.isoformat())
            # The same day as `since`: a bare date starts at midnight there.
            self.assertIn(
                self.wrong.id,
                [item["id"] for item in self.items(self.root, since=day.isoformat())],
            )

    def test_the_page_costs_the_same_queries_however_long(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.items(self.root)
            return len(queries.captured_queries)

        before = count()
        for _ in range(5):
            rate(
                ask(self.physics, citations=[{"document_id": self.lab.id}]),
                Feedback.Rating.INCORRECT,
            )
        self.assertEqual(count(), before)


class FeedbackAdminTests(TestCase):
    def test_feedback_is_readable_but_not_writable_in_django_admin(self):
        model_admin = admin.site._registry[Feedback]
        request = type(
            "Request", (), {"user": make_user("fb_admin", Role.SYSTEM_ADMIN, "")}
        )
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))


class MyFeedbackTests(TestCase):
    def setUp(self):
        self.user = make_user("mf_user", Role.STUDENT, "Physics")
        self.answer = ask(self.user)
        self.other = Message.objects.create(
            conversation=self.answer.conversation,
            role=Message.Role.ASSISTANT,
            content="Another answer",
        )

    def conversation(self):
        response = client_for(self.user).get(
            reverse("conversation-detail", args=[self.answer.conversation_id])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {message["id"]: message for message in response.json()["messages"]}

    def test_a_rated_answer_stays_rated(self):
        self.assertIsNone(self.conversation()[self.answer.id]["my_feedback"])
        client_for(self.user).post(
            reverse("message-feedback", args=[self.answer.id]),
            {"rating": "INCORRECT", "comment": "Wrong year"},
            format="json",
        )
        messages = self.conversation()
        self.assertEqual(
            messages[self.answer.id]["my_feedback"],
            {"rating": "INCORRECT", "comment": "Wrong year"},
        )
        self.assertIsNone(messages[self.other.id]["my_feedback"])

    def test_one_query_for_every_answers_feedback(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.conversation()
            return len(queries.captured_queries)

        before = count()
        for _ in range(4):
            rate(
                Message.objects.create(
                    conversation=self.answer.conversation,
                    role=Message.Role.ASSISTANT,
                    content="More",
                ),
                Feedback.Rating.HELPFUL,
            )
        self.assertEqual(count(), before)


class DashboardActivityTests(AnalyticsTestCase):
    def dashboard(self, user):
        response = client_for(user).get(reverse("dashboard"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_a_member_gets_their_own_activity_and_no_org_block(self):
        document(self.physics, "Lab rules")
        document(self.physics, "Old rules", days_ago=20)
        document(
            self.chemistry, "Salary revisions", document_type=Document.DocumentType.FORM
        )
        ask(self.physics, days_ago=3)
        ask(self.physics, abstained=True, reason=Abstention.NO_DOCUMENTS)
        ask(self.physics_admin)  # someone else's question

        activity = self.dashboard(self.physics)["activity"]
        self.assertIsNone(activity["org"])
        weeks = activity["uploads_by_week"]
        self.assertEqual(len(weeks), 12)
        self.assertEqual(weeks[-1]["count"], 1)
        self.assertEqual(sum(week["count"] for week in weeks), 2)
        monday = timezone.localdate(timezone.now(), analytics.analytics_zone())
        monday -= timedelta(days=monday.weekday())
        self.assertEqual(weeks[-1]["week"], monday.isoformat())

        days = activity["questions_by_day"]
        self.assertEqual(len(days), 30)
        self.assertEqual(days[-1], {"date": day_of(0), "count": 1, "answered": 0})
        self.assertEqual(days[-4], {"date": day_of(3), "count": 1, "answered": 1})

        # Only what this reader may open: no chemistry document anywhere.
        self.assertEqual(
            activity["documents_by_type"], [{"document_type": "POLICY", "count": 2}]
        )
        self.assertEqual(
            activity["documents_by_department"], [{"department": "Physics", "count": 2}]
        )

    def test_an_administrator_gets_a_week_of_the_organisation(self):
        ask(self.physics)
        ask(self.chemistry)
        rate(ask(self.physics), Feedback.Rating.HELPFUL)

        org = self.dashboard(self.physics_admin)["activity"]["org"]
        self.assertEqual(org["range"]["days"], 7)
        self.assertEqual(org["scope"], {"department": "Physics"})
        self.assertEqual(
            set(org["kpis"]),
            {"questions", "answer_rate", "helpful_rate", "documents_processed"},
        )
        self.assertEqual(org["kpis"]["questions"]["value"], 2)
        self.assertEqual(len(org["kpis"]["questions"]["series"]), 7)
        self.assertEqual(org["kpis"]["helpful_rate"]["value"], 1.0)
        root = self.dashboard(self.root)["activity"]["org"]
        self.assertEqual(root["kpis"]["questions"]["value"], 3)

    def test_the_activity_block_is_cached_between_polls(self):
        self.dashboard(self.physics)
        document(self.physics, "Lab rules")
        activity = self.dashboard(self.physics)["activity"]
        self.assertEqual(sum(week["count"] for week in activity["uploads_by_week"]), 0)
        # The live counts beside it are not cached.
        self.assertEqual(self.dashboard(self.physics)["documents"]["total"], 1)
