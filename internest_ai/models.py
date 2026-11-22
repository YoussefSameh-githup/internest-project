from django.conf import settings
from django.db import models
from django.utils import timezone


class AIUsage(models.Model):
    """One AI attempt (or cache hit): who asked, for which task, which provider answered, tokens and latency."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="ai_usage")
    partner = models.ForeignKey("internest_core.PartnerProfile", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="ai_usage")
    task = models.CharField(max_length=60)
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=120, blank=True)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)
    total_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    cached = models.BooleanField(default=False)
    error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI usage"
        verbose_name_plural = "AI usage"
        indexes = [
            models.Index(fields=["user", "task", "created_at"], name="aiusage_user_task_idx"),
            models.Index(fields=["partner", "created_at"], name="aiusage_partner_idx"),
            models.Index(fields=["provider", "created_at"], name="aiusage_provider_idx"),
        ]

    def __str__(self):
        return f"{self.task} · {self.provider} · {'ok' if self.ok else 'failed'}"


class AIQuotaCounter(models.Model):
    """A fixed-window counter (per user+task+day, partner+day, global minute / day). Incremented with one conditional
    UPDATE (used < limit), so concurrent requests can never overshoot a cap."""

    key = models.CharField(max_length=160, unique=True)
    used = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.key}: {self.used}"
