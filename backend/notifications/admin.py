from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only: a notification is written by the producers, through the
    service that keeps it plain and bounded, and an operator looking into
    "I was never told" needs to see what was sent, not to send things."""

    list_display = ("id", "recipient", "kind", "title", "updated_at", "read_at")
    list_filter = ("kind",)
    search_fields = ("recipient__username", "title")
    date_hierarchy = "updated_at"
    raw_id_fields = ("recipient",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
