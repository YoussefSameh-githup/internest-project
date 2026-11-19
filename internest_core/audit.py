"""Security audit trail (Phase 7): who opened a private file, activated an order, decided a review, exported or deleted data.

    record(request, action, target, **detail)   append one SecurityEvent; never raises (an audit hiccup must not break the page)
    purge_old_events()                          delete events older than settings.SECURITY_EVENT_RETENTION_DAYS

Events hold ids and paths, never document contents, passwords or codes. Staff read them in the admin (read-only).
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 365


def _ip(request):
    from .ratelimit import client_ip
    return client_ip(request) if request is not None else None


def record(request, action, target="", actor=None, **detail):
    from .models import SecurityEvent
    user = actor if actor is not None else getattr(request, "user", None)
    try:
        ip = _ip(request)
        SecurityEvent.objects.create(
            actor=user if user is not None and user.is_authenticated else None,
            action=action, target=str(target)[:200],
            ip=ip if ip and ip != "unknown" else None, detail=detail,
        )
    except Exception:  # noqa: BLE001 - logging must never take a page down
        logger.exception("Could not record security event %s %s", action, target)


def retention_days() -> int:
    return int(getattr(settings, "SECURITY_EVENT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def purge_old_events() -> int:
    from .models import SecurityEvent
    cutoff = timezone.now() - timedelta(days=retention_days())
    deleted, _ = SecurityEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted
