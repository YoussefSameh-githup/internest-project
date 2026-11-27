"""Periodic founder alerts + the daily summary (run by `manage.py founder_watch` every ~10 minutes).

Each alert has a dedupe key per hour (or per day), so a persistent problem produces one inbox line with a growing
counter and at most one email per 30 minutes, never a flood."""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from . import events, health, metrics

RETENTION = timedelta(days=90)


def _hour():
    return timezone.now().strftime("%Y-%m-%dT%H")


def check_alerts(prefs) -> list:
    from internest_core.models import APIIncident, AsyncTask

    raised = []
    snap = health.snapshot(use_cache=False)
    by_key = {c["key"]: c for c in snap["checks"]}

    def alert(kind, severity, title, link, key, data):
        events.emit(kind, data, dedupe_key=f"{kind}:{key}", notify_as=(severity, "system", title, link))
        raised.append(kind)

    if by_key["worker"]["state"] == health.UNAVAILABLE:
        alert("system.worker_down", "critical", "Background worker is not running", "/founder/system/", _hour(), {"detail": "no heartbeat"})
    if snap["queue"]["depth"] >= prefs.queue_backlog_alert:
        alert("system.queue_backlog", "warning", "Background queue backlog", "/founder/system/", _hour(), {"queued": snap["queue"]["depth"]})
    recent_dead = AsyncTask.objects.filter(status=AsyncTask.STATUS_DEAD, finished_at__gte=timezone.now() - timedelta(hours=1)).count()
    if recent_dead >= 3:
        alert("system.worker_failures", "warning", "Repeated background job failures", "/founder/system/", _hour(), {"dead_last_hour": recent_dead})
    if snap["disk_percent"] is not None and snap["disk_percent"] >= prefs.disk_alert_percent:
        severity = "critical" if snap["disk_percent"] >= 95 else "warning"
        alert("system.disk_space", severity, "Disk space is running low", "/founder/system/", _hour(), {"used_percent": round(snap["disk_percent"])})
    if by_key["backups"]["state"] in (health.DEGRADED, health.UNAVAILABLE):
        alert("system.backup_stale", "warning", "No recent database backup", "/founder/system/", timezone.localdate().isoformat(), {"state": by_key["backups"]["state"]})
    api_errors = APIIncident.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count()
    if api_errors >= 5:
        alert("system.api_errors", "warning", "Repeated external API errors", "/founder/system/", _hour(), {"incidents_last_hour": api_errors})
    return raised


def daily_summary(prefs, now=None) -> bool:
    """Once a day after 07:00 local time (the dedupe key makes a second run a no-op)."""
    now = timezone.localtime(now or timezone.now())
    if not prefs.daily_summary or now.hour < 7:
        return False
    data = metrics.overview(1)
    t, g = data["totals"], data["growth"]
    summary = {"new_users": g["users"]["current"], "new_applications": g["applications"]["current"],
               "new_internships": g["internships"]["current"], "payments": g["payments"]["current"],
               "open_internships": t["open_internships"], "pending_companies": t["pending_companies"],
               "orders_to_verify": t["orders_to_verify"]}
    event = events.emit("summary.daily", summary, dedupe_key=f"summary:{now.date().isoformat()}")
    if event is None:
        return False  # already sent today
    if prefs.recipient():
        lines = "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in summary.items())
        site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
        send_mail(f"[Internest] Daily summary {now.date()}", f"Last 24 hours\n\n{lines}\n\n{site}/founder/", None, [prefs.recipient()])
    return True


def purge() -> int:
    from .models import AutomationEvent, FounderNotification

    cutoff = timezone.now() - RETENTION
    a, _ = FounderNotification.objects.filter(read_at__isnull=False, last_seen__lt=cutoff).delete()
    b, _ = AutomationEvent.objects.filter(created_at__lt=cutoff).exclude(status=AutomationEvent.FAILED).delete()
    return a + b
