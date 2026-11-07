"""The notifications API: a person's own notifications, and the attention
counts for administrators.

Owner-scoped by construction - every query starts from request.user - so
someone else's notification is a 404, indistinguishable from one that does
not exist. Nothing here writes an audit row: these are reads and personal
read-state, polled by every open tab, and a trail of them would bury the
events the audit log exists for.
"""

from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import exceptions, serializers, status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsAdministrator

from . import attention, service
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        # Not the payload (producer bookkeeping) or the dedupe key.
        fields = [
            "id",
            "kind",
            "title",
            "body",
            "url",
            "created_at",
            "updated_at",
            "read_at",
        ]
        read_only_fields = fields


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 50


class Throttled:
    """The site-wide per-user limit, plus a scope of their own: a bell that
    polls should not be able to spend the budget the rest of the app runs
    on, and a misbehaving client polling in a loop should hit a wall here
    first."""

    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "notifications"


class NotificationListView(Throttled, ListAPIView):
    """GET /notifications/?unread=1&page=N - newest news first."""

    serializer_class = NotificationSerializer
    pagination_class = NotificationPagination

    def get_queryset(self):
        mine = Notification.objects.filter(recipient=self.request.user)
        if self.request.query_params.get("unread") in ("1", "true"):
            mine = mine.filter(read_at__isnull=True)
        return mine.order_by("-updated_at", "-id")


class NotificationSummaryView(Throttled, APIView):
    """GET /notifications/summary/ - what every open tab polls.

    `latest_at` is when the newest news arrived (or last changed, for a
    notification that coalesces), read or not; the client compares it with
    what it last saw to tell that something new came in.
    """

    def get(self, request):
        totals = Notification.objects.filter(recipient=request.user).aggregate(
            unread=Count("id", filter=Q(read_at__isnull=True)),
            latest_at=Max("updated_at"),
        )
        return Response({"unread": totals["unread"], "latest_at": totals["latest_at"]})


class NotificationReadView(Throttled, APIView):
    """POST /notifications/{id}/read/ - 204, or 404 for one that is not yours."""

    def post(self, request, pk):
        if not service.mark_read(request.user, pk):
            raise exceptions.NotFound()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationReadAllView(Throttled, APIView):
    """POST /notifications/read-all/ - {"updated": n}.

    Optional body {"until": iso}: only news no newer than that, so what
    arrived after the person looked stays unread.
    """

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        raw = data.get("until")
        until = None
        if raw not in (None, ""):
            try:
                until = parse_datetime(raw) if isinstance(raw, str) else None
            except ValueError:
                # Well-formed but impossible, e.g. month 13.
                until = None
            if until is None:
                raise exceptions.ValidationError(
                    {"until": ["An ISO 8601 date and time, e.g. 2026-11-07T09:30:00Z."]}
                )
            if timezone.is_naive(until):
                until = timezone.make_aware(until)
        return Response({"updated": service.mark_all_read(request.user, until=until)})


class AttentionView(Throttled, APIView):
    """GET /admin/attention/ - counts for the sidebar badges (administrators)."""

    permission_classes = [IsAdministrator]

    def get(self, request):
        return Response(attention.attention_for(request.user))
