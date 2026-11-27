"""Business events: the founder inbox and the optional n8n outbox, from one call.

    emit(kind, data, *, dedupe_key=None, notify=None)
        records an AutomationEvent (delivered to n8n by the async worker after the commit) and, with
        `notify=(severity, category, title, link)`, folds a FounderNotification into the inbox.
    notify(severity, category, title, *, body="", link="", dedupe_key)
        inbox only. Critical alerts are also emailed (once per 30 min per alert) when enabled.

Never raises into the caller: registration, applications and payments must not fail because of this module.
Payloads carry ids, counts and statuses only (no emails, names, phone numbers or secrets).

Webhook format (n8n "Webhook" node, POST, raw body):
    headers  X-Internest-Event, X-Internest-Event-Id, X-Internest-Timestamp,
             X-Internest-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
    body     {"id": "<uuid>", "type": "<kind>", "created": "<ISO 8601>", "data": {...}}
The receiver must recompute the signature, reject timestamps older than 5 minutes and drop event ids it already
processed (replay protection). Retries reuse the same id, so deduplicating on it makes delivery idempotent.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Inbox titles are stored in English (data stays language-neutral) and translated when shown ({% trans n.title %}).
# This list only registers them in the translation catalog; it is never evaluated.
_TITLES = [_("Staff or superuser access granted"), _("New student registrations today"), _("New company awaiting verification"),
           _("Opportunities published today"), _("Payment receipt to verify"), _("Payment confirmed"), _("Payment order cancelled"),
           _("Milestone reached: users"), _("Milestone reached: applications"), _("n8n webhook delivery failed"),
           _("Website is not answering (2 probes failed)"), _("Background worker is not running"), _("Background queue backlog"),
           _("Repeated background job failures"), _("Disk space is running low"), _("No recent database backup"),
           _("Repeated external API errors")]

PROVIDER = "n8n"
DELIVER_JOB = "founder_webhook"
EMAIL_JOB = "founder_email"
MAX_ATTEMPTS = 6
MAX_BODY = 16 * 1024
TIMEOUT = 10.0
EMAIL_COOLDOWN = timedelta(minutes=30)
SIGNATURE_TOLERANCE = 300  # seconds; documented for receivers, enforced by verify()


# ---------------------------------------------------------------- configuration
def webhook_url() -> str:
    url = (getattr(settings, "N8N_WEBHOOK_URL", "") or "").strip()
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.hostname else ""


def webhook_secret() -> str:
    return getattr(settings, "N8N_WEBHOOK_SECRET", "") or ""


def is_configured() -> bool:
    return bool(webhook_url() and len(webhook_secret()) >= 32)


def target_host() -> str:
    """Shown in the UI instead of the full URL (a webhook path can itself be a secret)."""
    return urlsplit(webhook_url()).hostname or ""


# ---------------------------------------------------------------- signing
def body_for(event) -> bytes:
    doc = {"id": str(event.event_id), "type": event.kind, "created": event.created_at.isoformat(), "data": event.payload}
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(body: bytes, timestamp: int, secret: str | None = None) -> str:
    key = (secret if secret is not None else webhook_secret()).encode("utf-8")
    return "v1=" + hmac.new(key, f"{timestamp}.".encode("ascii") + body, hashlib.sha256).hexdigest()


def verify(body: bytes, timestamp: str, signature: str, secret: str, now: float | None = None) -> bool:
    """Reference check for receivers (and the tests): signature valid AND timestamp fresh."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE:
        return False
    return hmac.compare_digest(sign(body, ts, secret), signature or "")


def headers_for(event, body: bytes, timestamp: int) -> dict:
    return {"Content-Type": "application/json; charset=utf-8", "User-Agent": "Internest-Webhooks/1",
            "X-Internest-Event": event.kind, "X-Internest-Event-Id": str(event.event_id),
            "X-Internest-Timestamp": str(timestamp), "X-Internest-Signature": sign(body, timestamp)}


# ---------------------------------------------------------------- inbox
def notify(severity, category, title, *, body="", link="", dedupe_key) -> None:
    from .models import FounderNotification

    try:
        now = timezone.now()
        with transaction.atomic():
            updated = FounderNotification.objects.filter(dedupe_key=dedupe_key[:120]).update(
                count=F("count") + 1, last_seen=now, read_at=None, title=title[:160], body=body[:300])
            if not updated:
                try:
                    with transaction.atomic():
                        FounderNotification.objects.create(severity=severity, category=category, title=title[:160],
                                                           body=body[:300], link=link[:200], dedupe_key=dedupe_key[:120])
                except IntegrityError:  # a concurrent writer created it first
                    pass
        if severity == FounderNotification.CRITICAL:
            transaction.on_commit(lambda: _queue_email(dedupe_key[:120]))
    except Exception:
        logger.exception("Founder notification %s failed", dedupe_key)


def _queue_email(dedupe_key: str) -> None:
    from internest_core.async_tasks import enqueue

    try:
        enqueue(EMAIL_JOB, None, idempotency_key=f"founder-email:{dedupe_key}", max_attempts=3, dedupe_key=dedupe_key)
    except Exception:
        logger.exception("Could not queue the founder alert email")


def send_alert_email(dedupe_key: str) -> tuple[int, dict]:
    """Worker side. Respects the founder's switch and a 30-minute cooldown per alert."""
    from django.core.mail import send_mail

    from .models import FounderNotification, FounderSettings

    prefs = FounderSettings.load()
    note = FounderNotification.objects.filter(dedupe_key=dedupe_key).first()
    if note is None or not prefs.email_critical or not prefs.recipient():
        return 200, {"emailed": False, "reason": "disabled"}
    if note.emailed_at and timezone.now() - note.emailed_at < EMAIL_COOLDOWN:
        return 200, {"emailed": False, "reason": "cooldown"}
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    send_mail(f"[Internest] {note.title}", f"{note.body}\n\n{site}{note.link or '/founder/notifications/'}\n"
              f"(seen {note.count} time(s))", None, [prefs.recipient()])
    FounderNotification.objects.filter(pk=note.pk).update(emailed_at=timezone.now())
    return 200, {"emailed": True}


# ---------------------------------------------------------------- outbox
def emit(kind: str, data: dict | None = None, *, dedupe_key: str | None = None, notify_as=None):
    """Record one business event. `notify_as=(severity, category, title, link)` also files an inbox item."""
    from .models import AutomationEvent

    try:
        status = AutomationEvent.PENDING if is_configured() else AutomationEvent.SKIPPED
        try:
            with transaction.atomic():
                event = AutomationEvent.objects.create(kind=kind[:60], payload=_bounded(data or {}), status=status,
                                                       dedupe_key=dedupe_key[:120] if dedupe_key else None)
        except IntegrityError:  # the same milestone / alert was already recorded: no second event, no second alert
            return None
        if notify_as:
            severity, category, title, link = notify_as
            notify(severity, category, title, body=_summary(data), link=link, dedupe_key=dedupe_key or f"{kind}:{event.event_id}")
        if status == AutomationEvent.PENDING:
            transaction.on_commit(lambda: queue_delivery(event.pk))
        return event
    except Exception:
        logger.exception("Founder event %s failed", kind)
        return None


def _summary(data) -> str:
    return ", ".join(f"{k}={v}" for k, v in (data or {}).items() if isinstance(v, (int, float, str)) and len(str(v)) < 40)[:300]


def _bounded(data: dict) -> dict:
    text = json.dumps(data, default=str)
    return data if len(text) <= MAX_BODY - 512 else {"truncated": True}


def queue_delivery(event_pk) -> None:
    from internest_core.async_tasks import enqueue

    try:
        enqueue(DELIVER_JOB, None, idempotency_key=f"founder-webhook:{event_pk}", max_attempts=MAX_ATTEMPTS, event_pk=event_pk)
    except Exception:
        logger.exception("Could not queue webhook delivery for event %s", event_pk)


def deliver(event_pk) -> tuple[int, dict]:
    """Worker side: POST one event. 2xx done; 408/429/5xx/network retried with backoff; other 4xx failed for good."""
    from internest_core import http_client
    from internest_core.api_monitor import monitored_api_call
    from internest_core.async_tasks import RetryTask

    from .models import AutomationEvent

    event = AutomationEvent.objects.filter(pk=event_pk).first()
    if event is None or event.status == AutomationEvent.DELIVERED:
        return 200, {"delivered": True}
    if not is_configured():
        AutomationEvent.objects.filter(pk=event.pk).update(status=AutomationEvent.SKIPPED)
        return 200, {"delivered": False, "reason": "not_configured"}
    attempts = event.attempts + 1
    body = body_for(event)
    try:
        response = monitored_api_call(PROVIDER, DELIVER_JOB, http_client.post, webhook_url(), content=body,
                                      headers=headers_for(event, body, int(time.time())), timeout=TIMEOUT)
    except Exception as exc:  # network / timeout
        return _failed(event, attempts, None, f"{type(exc).__name__}", retry=True, retry_exc=RetryTask)
    code = response.status_code
    if 200 <= code < 300:
        AutomationEvent.objects.filter(pk=event.pk).update(status=AutomationEvent.DELIVERED, attempts=attempts,
                                                           last_status_code=code, last_error="", delivered_at=timezone.now())
        return 200, {"delivered": True}
    return _failed(event, attempts, code, f"HTTP {code}", retry=code in (408, 429) or code >= 500, retry_exc=RetryTask)


def _failed(event, attempts, code, error, *, retry, retry_exc):
    from .models import AutomationEvent

    final = not retry or attempts >= MAX_ATTEMPTS
    AutomationEvent.objects.filter(pk=event.pk).update(attempts=attempts, last_status_code=code, last_error=error[:200],
                                                       status=AutomationEvent.FAILED if final else AutomationEvent.PENDING)
    if final:
        notify("warning", "automation", "n8n webhook delivery failed", body=f"event={event.kind}, last={error}",
               link="/founder/automations/", dedupe_key=f"webhook-failed:{timezone.localdate()}")
        return 502, {"delivered": False, "error": error, "code": "webhook_failed"}
    raise retry_exc(error)


def retry(event) -> bool:
    """Founder action: put a failed event back in the queue (same event id, so receivers stay idempotent)."""
    from .models import AutomationEvent

    if event.status != AutomationEvent.FAILED or not is_configured():
        return False
    AutomationEvent.objects.filter(pk=event.pk).update(status=AutomationEvent.PENDING, attempts=0)
    transaction.on_commit(lambda: queue_delivery(event.pk))
    return True


# ---------------------------------------------------------------- milestones
def milestone(name: str, total: int, step: int, link: str) -> None:
    """Fires once per threshold (50, 100, 150 ... users): the dedupe key is the level, not the exact count."""
    if step <= 0 or total < step:
        return
    level = (total // step) * step
    emit(f"milestone.{name}", {"metric": name, "reached": level}, dedupe_key=f"milestone:{name}:{level}",
         notify_as=("milestone", "business", f"Milestone reached: {name}", link))
