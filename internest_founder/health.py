"""System health for /founder/system/: bounded, read-only checks; no shell commands, paths, IPs or secrets.

Each check -> {"key", "label", "state", "detail", "checked_at"} with state healthy | degraded | unavailable | unknown.
"unknown" is used whenever this process cannot really observe something (e.g. nginx from inside Django, memory on
a platform without /proc), so nothing is shown as healthy on stale or missing evidence. The snapshot is cached 30 s.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.translation import gettext as _

HEALTHY, DEGRADED, UNAVAILABLE, UNKNOWN = "healthy", "degraded", "unavailable", "unknown"
CACHE_KEY = "founder:health:v1"
CACHE_SECONDS = 30
PROBE_FRESH = timedelta(hours=2)
BACKUP_FRESH = timedelta(hours=26)
WORKER_FRESH = 90  # seconds since the last heartbeat


def _check(key, label, state, detail):
    return {"key": key, "label": label, "state": state, "detail": detail, "checked_at": timezone.now()}


def database():
    started = time.perf_counter()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return _check("database", _("Database"), UNAVAILABLE, _("Query failed"))
    ms = (time.perf_counter() - started) * 1000
    return _check("database", _("Database"), HEALTHY if ms < 200 else DEGRADED, _("%(ms).0f ms round trip") % {"ms": ms})


def queue(prefs):
    from internest_core.models import AsyncTask

    now = timezone.now()
    queued = AsyncTask.objects.filter(status=AsyncTask.STATUS_QUEUED)
    depth = queued.count()
    oldest = queued.filter(next_run_at__lte=now).order_by("created_at").values_list("created_at", flat=True).first()
    day = AsyncTask.objects.filter(finished_at__gte=now - timedelta(hours=24))
    failed = day.filter(status__in=(AsyncTask.STATUS_FAILED, AsyncTask.STATUS_DEAD)).count()
    wait = (now - oldest).total_seconds() if oldest else 0
    state = HEALTHY
    if depth >= prefs.queue_backlog_alert or wait > 600:
        state = DEGRADED
    if failed >= 10:
        state = DEGRADED
    detail = _("%(depth)s queued, oldest waiting %(wait)s s, %(failed)s failed or dead in 24 h") % {
        "depth": depth, "wait": int(wait), "failed": failed}
    return _check("queue", _("Background queue"), state, detail), {"depth": depth, "wait": wait, "failed": failed}


def worker(queue_stats):
    from internest_core import async_tasks

    if async_tasks.backend() != "db":
        return _check("worker", _("Background worker"), UNKNOWN, _("Queue backend is %(b)s, no separate worker") % {"b": async_tasks.backend()})
    beat = async_tasks.last_heartbeat()
    if beat is None:
        state = UNAVAILABLE if queue_stats["wait"] > 120 else UNKNOWN
        return _check("worker", _("Background worker"), state, _("No heartbeat recorded"))
    age = time.time() - beat
    if age <= WORKER_FRESH:
        return _check("worker", _("Background worker"), HEALTHY, _("Heartbeat %(s)s s ago") % {"s": int(age)})
    return _check("worker", _("Background worker"), UNAVAILABLE, _("Last heartbeat %(m)s min ago") % {"m": int(age // 60)})


def disk(prefs):
    try:
        usage = shutil.disk_usage(settings.BASE_DIR)
    except OSError:
        return _check("disk", _("Disk"), UNKNOWN, _("Not measurable here")), None
    percent = usage.used * 100 / usage.total
    state = HEALTHY if percent < prefs.disk_alert_percent else (UNAVAILABLE if percent >= 97 else DEGRADED)
    return _check("disk", _("Disk"), state, _("%(p).0f%% used, %(free).1f GB free") % {
        "p": percent, "free": usage.free / 1e9}), percent


def memory():
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            info = {line.split(":")[0]: int(line.split()[1]) for line in fh if line.split()[1:2]}
        total, available = info["MemTotal"], info["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return _check("memory", _("Memory"), UNKNOWN, _("Not measurable here"))
    free = available * 100 / total
    state = HEALTHY if free >= 15 else (DEGRADED if free >= 5 else UNAVAILABLE)
    return _check("memory", _("Memory"), state, _("%(a)s MB available of %(t)s MB") % {"a": available // 1024, "t": total // 1024})


def cpu():
    try:
        load1, _load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        return _check("cpu", _("CPU"), UNKNOWN, _("Not measurable here"))
    cores = os.cpu_count() or 1
    state = HEALTHY if load15 < cores else (DEGRADED if load15 < cores * 2 else UNAVAILABLE)
    return _check("cpu", _("CPU"), state, _("Load %(l1).2f (1 min), %(l15).2f (15 min) on %(c)s cores") % {
        "l1": load1, "l15": load15, "c": cores})


def backups():
    from datetime import datetime, timezone as dt_timezone

    from internest_core.backups import list_backups

    try:
        items = list_backups()
        newest = datetime.fromtimestamp(items[-1].stat().st_mtime, tz=dt_timezone.utc) if items else None
    except Exception:
        return _check("backups", _("Backups"), UNKNOWN, _("Backup folder not readable"))
    if newest is None:
        return _check("backups", _("Backups"), UNAVAILABLE, _("No backup found: schedule manage.py backup_data"))
    age = timezone.now() - newest
    state = HEALTHY if age <= BACKUP_FRESH else DEGRADED
    return _check("backups", _("Backups"), state, _("Newest %(h)s h old, %(n)s kept") % {
        "h": int(age.total_seconds() // 3600), "n": len(items)})


def website():
    from . import perf
    from .models import PerformanceMeasurement

    label = _("Public website (via Cloudflare)")
    last = PerformanceMeasurement.objects.filter(source=PerformanceMeasurement.PROBE, url=perf.absolute("/")).first()
    if last is None:
        return _check("website", label, UNKNOWN, _("No probe yet: run manage.py founder_probe"))
    if timezone.now() - last.measured_at > PROBE_FRESH:
        return _check("website", label, UNKNOWN, _("Last probe is stale (%(t)s)") % {
            "t": timezone.localtime(last.measured_at).strftime("%Y-%m-%d %H:%M")})
    if last.status_code == 200:
        slow = bool(last.ttfb_ms and last.ttfb_ms > 1500)
        return _check("website", label, DEGRADED if slow else HEALTHY, _("HTTP 200, %(ms)s ms to first byte") % {"ms": last.ttfb_ms})
    return _check("website", label, UNAVAILABLE, last.error or f"HTTP {last.status_code}")


def web_process():
    return _check("web", _("Web application (Gunicorn + Django)"), HEALTHY, _("Answered this request"))


def external_apis():
    from internest_core.models import APIIncident

    since = timezone.now() - timedelta(hours=24)
    try:
        rows = list(APIIncident.objects.filter(created_at__gte=since).values_list("provider_id", flat=True)[:500])
    except Exception:
        return _check("apis", _("External APIs"), UNKNOWN, _("Incident log not readable"))
    if not rows:
        return _check("apis", _("External APIs"), HEALTHY, _("No incident in 24 h"))
    providers = len(set(rows))
    return _check("apis", _("External APIs"), DEGRADED, _("%(n)s incident(s) in 24 h across %(p)s provider(s)") % {
        "n": len(rows), "p": providers})


def error_tracking():
    dsn = bool(getattr(settings, "SENTRY_DSN", "") or os.getenv("SENTRY_DSN"))
    return _check("errors", _("Error tracking (Sentry)"), HEALTHY if dsn else DEGRADED,
                  _("Configured") if dsn else _("Not configured: errors are only in server logs"))


def snapshot(use_cache=True) -> dict:
    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached:
            return cached
    from .models import FounderSettings

    prefs = FounderSettings.load()
    queue_check, stats = queue(prefs)
    disk_check, disk_percent = disk(prefs)
    checks = [website(), web_process(), database(), worker(stats), queue_check, disk_check, memory(), cpu(), backups(),
              external_apis(), error_tracking()]
    order = [HEALTHY, UNKNOWN, DEGRADED, UNAVAILABLE]
    overall = max((c["state"] for c in checks if c["key"] not in ("errors",)), key=order.index)
    data = {"checks": checks, "overall": overall, "queue": stats, "disk_percent": disk_percent, "at": timezone.now()}
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data
