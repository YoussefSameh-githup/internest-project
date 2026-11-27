"""Founder Control Center storage. Business data stays in its own apps; these tables hold only what the founder
center adds: its settings, the notification inbox, the n8n outbox and performance measurements."""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class FounderSettings(models.Model):
    """One row (pk=1). Thresholds and alert routing the founder edits in /founder/settings/."""

    alert_email = models.EmailField(blank=True, help_text=_("Empty: ADMIN_ALERT_EMAIL from .env."))
    email_critical = models.BooleanField(default=True, help_text=_("Email critical alerts (deduplicated)."))
    daily_summary = models.BooleanField(default=False, help_text=_("Email a daily founder summary."))
    users_milestone_step = models.PositiveIntegerField(default=50)
    applications_milestone_step = models.PositiveIntegerField(default=100)
    queue_backlog_alert = models.PositiveIntegerField(default=50, help_text=_("Queued tasks before an alert."))
    disk_alert_percent = models.PositiveSmallIntegerField(default=85)
    slow_page_ms = models.PositiveIntegerField(default=800, help_text=_("Server response time (TTFB) that counts as slow."))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Founder settings")
        verbose_name_plural = _("Founder settings")
        permissions = [("access_control_center", "Can open the Founder Control Center")]

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def recipient(self) -> str:
        return self.alert_email or getattr(settings, "ADMIN_ALERT_EMAIL", "") or ""


class FounderNotification(models.Model):
    """Founder inbox. `dedupe_key` folds repeats of the same alert into one row (count + last_seen)."""

    CRITICAL, WARNING, MILESTONE, INFO = "critical", "warning", "milestone", "info"
    SEVERITIES = [(CRITICAL, _("Critical")), (WARNING, _("Warning")), (MILESTONE, _("Milestone")), (INFO, _("Info"))]
    CATEGORIES = [("business", _("Business")), ("system", _("System")), ("automation", _("Automation")),
                  ("security", _("Security")), ("performance", _("Performance"))]

    severity = models.CharField(max_length=10, choices=SEVERITIES, default=INFO, db_index=True)
    category = models.CharField(max_length=12, choices=CATEGORIES, default="business", db_index=True)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=300, blank=True)  # ids and counts only, never personal data
    link = models.CharField(max_length=200, blank=True)  # a /founder/ path
    dedupe_key = models.CharField(max_length=120, unique=True)
    count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.severity}: {self.title}"


class AutomationEvent(models.Model):
    """Outbox for n8n: one row per business event, delivered by the async worker with signed webhooks."""

    PENDING, DELIVERED, FAILED, SKIPPED = "pending", "delivered", "failed", "skipped"
    STATUSES = [(PENDING, _("Pending")), (DELIVERED, _("Delivered")), (FAILED, _("Failed")), (SKIPPED, _("Not configured"))]

    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=60, db_index=True)
    dedupe_key = models.CharField(max_length=120, unique=True, null=True, blank=True)
    payload = models.JSONField(default=dict)  # ids, counts and statuses; no emails, names or secrets
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class PerformanceMeasurement(models.Model):
    """A probe (server-side HTTP timing through Cloudflare) or a lab run (Lighthouse JSON import)."""

    PROBE, LAB = "probe", "lab"
    SOURCES = [(PROBE, _("Synthetic probe")), (LAB, _("Lab (Lighthouse)"))]

    source = models.CharField(max_length=6, choices=SOURCES, db_index=True)
    url = models.CharField(max_length=300, db_index=True)
    measured_at = models.DateTimeField(db_index=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    ttfb_ms = models.PositiveIntegerField(null=True, blank=True)
    total_ms = models.PositiveIntegerField(null=True, blank=True)
    bytes = models.PositiveIntegerField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)  # headers summary / Lighthouse scores, vitals, opportunities
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-measured_at"]
        indexes = [models.Index(fields=["source", "url", "-measured_at"])]


class WebVitalDaily(models.Model):
    """Field data (real visitors, sampled): one counter row per day x page x metric x device. No user, IP or URL ids."""

    METRICS = ("LCP", "INP", "CLS", "TTFB", "FCP")
    day = models.DateField()
    page = models.CharField(max_length=40)  # URL name, e.g. "landing", "internship_detail"
    metric = models.CharField(max_length=4)
    device = models.CharField(max_length=7)  # mobile | desktop
    good = models.PositiveIntegerField(default=0)
    needs_improvement = models.PositiveIntegerField(default=0)
    poor = models.PositiveIntegerField(default=0)
    total = models.FloatField(default=0)  # sum of values, for the mean

    class Meta:
        constraints = [models.UniqueConstraint(fields=["day", "page", "metric", "device"], name="founder_vital_day_uniq")]
        ordering = ["-day"]

    @property
    def samples(self):
        return self.good + self.needs_improvement + self.poor
