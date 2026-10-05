from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import exceptions, status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsAdministrator, IsSystemAdministrator
from documents import authorization, services
from documents.api import discovery
from documents.audit import record_event
from documents.models import AuditLog, Conversation, Document, ProcessingJob
from documents.processing.errors import ErrorCode

from . import health
from .serializers import AuditLogSerializer, OperationsJobSerializer

User = get_user_model()


def _department_scope(user):
    """None for a system admin (no restriction), else their department."""
    if authorization.is_system_admin(user):
        return None
    return authorization.get_profile(user).department


def _boundary(params, name, *, end_of_day):
    value = params.get(name, "").strip()
    return (
        discovery.parse_boundary(value, name, end_of_day=end_of_day) if value else None
    )


class AuditLogView(ListAPIView):
    """The audit trail, newest first.

    A department administrator sees rows about their department: actions
    taken BY its members, and actions taken ON its documents. The second
    half matters as much as the first - an outsider's denied attempt to
    read a department's document is exactly what that department's admin
    needs to see.
    """

    serializer_class = AuditLogSerializer
    permission_classes = [IsAdministrator]

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("user", "document").order_by(
            "-created_at", "-id"
        )

        department = _department_scope(self.request.user)
        # A document of the department: its current department while it
        # exists, the one recorded on the row once it has been deleted.
        in_department = Q(document__department=department) | Q(
            document__isnull=True,
            document_ref__isnull=False,
            document_department=department,
        )
        if department is not None:
            queryset = queryset.filter(
                Q(user__profile__department=department) | in_department
            )

        params = self.request.query_params
        if params.get("action"):
            queryset = queryset.filter(action=params["action"])
        if params.get("result"):
            queryset = queryset.filter(result=params["result"])
        # document_ref, not document_id: a deleted document's trail stays
        # queryable by its id.
        for name, field in (("user", "user_id"), ("document", "document_ref")):
            value = params.get(name, "")
            if value:
                if not value.isdigit():
                    raise exceptions.ValidationError({name: ["Must be an id."]})
                queryset = queryset.filter(**{field: int(value)})
        if params.get("document") and department is not None:
            # Filtering by another department's document id would answer
            # "does it exist?" through whether any row comes back, which the
            # serializer withholding the id cannot prevent.
            queryset = queryset.filter(in_department)

        since = _boundary(params, "since", end_of_day=False)
        until = _boundary(params, "until", end_of_day=True)
        if since is not None:
            queryset = queryset.filter(created_at__gte=since)
        if until is not None:
            queryset = queryset.filter(created_at__lte=until)
        return queryset

    def get_serializer_context(self):
        return {
            **super().get_serializer_context(),
            "department_scope": _department_scope(self.request.user),
        }


class JobListView(ListAPIView):
    """The processing monitor: failed, running and stalled jobs."""

    serializer_class = OperationsJobSerializer
    permission_classes = [IsAdministrator]

    def get_queryset(self):
        queryset = ProcessingJob.objects.select_related(
            "document_version__document"
        ).order_by("-updated_at", "-id")

        department = _department_scope(self.request.user)
        if department is not None:
            queryset = queryset.filter(
                document_version__document__department=department
            )

        params = self.request.query_params
        if params.get("status"):
            queryset = queryset.filter(status=params["status"])
        if params.get("error_code"):
            queryset = queryset.filter(error_code=params["error_code"])
        if params.get("stalled") == "true":
            queryset = queryset.filter(
                status=ProcessingJob.Status.PROCESSING,
                updated_at__lt=services.stalled_cutoff(),
            )
        return queryset

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "cutoff": services.stalled_cutoff()}


class JobRetryView(APIView):
    permission_classes = [IsAdministrator]

    def post(self, request, pk):
        job = (
            ProcessingJob.objects.select_related("document_version__document")
            .filter(pk=pk)
            .first()
        )
        department = _department_scope(request.user)
        if job is None or (
            department is not None
            and job.document_version.document.department != department
        ):
            raise exceptions.NotFound()

        try:
            replacement = services.retry_job(job)
        except services.ProcessingInProgress as exc:
            return Response(
                {"detail": str(exc), "job_id": exc.job.id},
                status=status.HTTP_409_CONFLICT,
            )
        except (services.DocumentArchived, services.DocumentQuarantined) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        record_event(
            user=request.user,
            action=AuditLog.Action.JOB_RETRY,
            document=job.document_version.document,
            result=AuditLog.Result.ALLOWED,
            job_id=job.id,
            replacement_job_id=replacement.id,
        )
        return Response(
            OperationsJobSerializer(
                replacement, context={"cutoff": services.stalled_cutoff()}
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class SystemStatusView(APIView):
    """What an operator needs to know is working, without any secret.

    Component NAMES only - which embedder, which reranker, which answerer -
    never a key, a URL with credentials or a bucket path. The same
    information is already in every evaluation run's stored configuration.
    """

    permission_classes = [IsSystemAdministrator]

    def get(self, request):
        from documents.evaluation.runner import capture_configuration

        now = timezone.now()
        day_ago = now - timedelta(hours=24)

        jobs = ProcessingJob.objects.filter(updated_at__gte=day_ago)
        job_counts = dict(
            jobs.values_list("status")
            .annotate(total=Count("id"))
            .values_list("status", "total")
        )

        document_counts = dict(
            Document.objects.filter(archived_at__isnull=True)
            .values_list("status")
            .annotate(total=Count("id"))
            .values_list("status", "total")
        )

        return Response(
            {
                "checked_at": now,
                "components": health.check_all(),
                "queues": health.queue_depths(),
                "documents": {
                    "by_status": document_counts,
                    "archived": Document.objects.filter(
                        archived_at__isnull=False
                    ).count(),
                },
                "jobs_last_24h": {
                    "by_status": job_counts,
                    "timeouts": jobs.filter(error_code=ErrorCode.TIMEOUT).count(),
                    "stalled_now": ProcessingJob.objects.filter(
                        status=ProcessingJob.Status.PROCESSING,
                        updated_at__lt=services.stalled_cutoff(),
                    ).count(),
                },
                "users": {
                    "active": User.objects.filter(is_active=True).count(),
                    "inactive": User.objects.filter(is_active=False).count(),
                },
                "ai": capture_configuration(),
                "pipeline": {
                    "classifier": bool(getattr(settings, "DOCUMENT_CLASSIFIER", "")),
                    "field_extractor": bool(
                        getattr(settings, "DOCUMENT_FIELD_EXTRACTOR", "")
                    ),
                    "scanner": bool(getattr(settings, "DOCUMENT_SCANNER", "")),
                    "object_storage": bool(
                        getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
                    ),
                    "soft_time_limit_seconds": getattr(
                        settings, "CELERY_TASK_SOFT_TIME_LIMIT", None
                    ),
                },
                "debug": settings.DEBUG,
            }
        )


class DashboardView(APIView):
    """One round trip for the signed-in user's home screen.

    Everything is computed over `visible_documents`, the same scope the
    listing uses, so the dashboard can never count a document its viewer
    could not open.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        visible = authorization.visible_documents(
            user, Document.objects.filter(archived_at__isnull=True)
        )

        by_status = dict(
            visible.values_list("status")
            .annotate(total=Count("id"))
            .values_list("status", "total")
        )

        recent = visible.select_related("current_version", "owner").order_by(
            "-updated_at", "-id"
        )[:6]
        attention = (
            visible.filter(
                status__in=(Document.Status.FAILED, Document.Status.QUARANTINED)
            )
            .select_related("current_version", "owner")
            .order_by("-updated_at", "-id")
        )
        # A failed document is only actionable by someone who may reprocess
        # it; showing everyone else a list of other people's failures is
        # noise.
        attention = [
            document
            for document in attention[:20]
            if authorization.can_manage_document(user, document)
        ][:6]

        conversations = (
            Conversation.objects.filter(owner=user)
            .annotate(message_count=Count("messages"))
            .order_by("-updated_at")[:5]
        )

        def summary(document):
            version = document.current_version
            return {
                "id": document.id,
                "title": document.title,
                "status": document.status,
                "document_type": document.document_type,
                "department": document.department,
                "updated_at": document.updated_at,
                "file_name": version.file_name if version else "",
            }

        return Response(
            {
                "documents": {
                    "total": sum(by_status.values()),
                    "by_status": by_status,
                },
                "recent_documents": [summary(document) for document in recent],
                "needs_attention": [summary(document) for document in attention],
                "recent_conversations": [
                    {
                        "id": conversation.id,
                        "title": conversation.title,
                        "updated_at": conversation.updated_at,
                        "message_count": conversation.message_count,
                    }
                    for conversation in conversations
                ],
            }
        )
