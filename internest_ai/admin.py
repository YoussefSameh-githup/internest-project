from django.contrib import admin

from internest_core.admin_base import BAD, OK, ModelAdmin, flag_badge

from .models import AIUsage


@admin.register(AIUsage)
class AIUsageAdmin(ModelAdmin):
    """Read-only usage log: which task used which provider, tokens, latency and failures."""

    list_display = ("created_at", "task", "provider", "model", "user", "partner", "total_tokens", "latency_ms", "status", "cached")
    list_filter = ("provider", "task", "ok", "cached")
    search_fields = ("task", "provider", "model", "user__username", "error")
    date_hierarchy = "created_at"
    list_select_related = ("user", "partner")
    readonly_fields = [f.name for f in AIUsage._meta.fields]

    @flag_badge("Result", "ok", "failed", OK, BAD)
    def status(self, obj):
        return obj.ok

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
