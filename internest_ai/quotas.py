"""AI usage caps and the usage log.

    reservation = reserve("cv_polish", user_id=7, partner_id=None)   # raises QuotaExceeded(scope, retry_after)
    ...call the AI...
    release(reservation)                                             # only when no provider answered (outage)

Caps (settings, 0 = unlimited): AI_QUOTA_USER_DAILY per user per task per day (AI_QUOTA_TASK_LIMITS overrides it per
task), AI_QUOTA_PARTNER_DAILY per company per day, AI_QUOTA_GLOBAL_PER_MINUTE and AI_QUOTA_GLOBAL_PER_DAY for the site.
Each cap is a fixed-window AIQuotaCounter row taken with one conditional UPDATE (`used < limit`), inside one transaction
for all the caps of a request: concurrent requests can never overshoot, and a refused request takes nothing.
Cache hits are free and never reserve. When the database itself is unavailable the check fails open (logged): a quota
problem must not take the AI features down.
"""
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from .errors import QuotaExceeded

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bucket:
    key: str
    limit: int
    scope: str
    expires_at: datetime

    def retry_after(self, now) -> int:
        return max(1, int((self.expires_at - now).total_seconds() + 0.999))


@dataclass
class Reservation:
    buckets: list = field(default_factory=list)


def enabled() -> bool:
    return bool(getattr(settings, "AI_QUOTAS_ENABLED", True))


def user_daily_limit(task: str) -> int:
    per_task = getattr(settings, "AI_QUOTA_TASK_LIMITS", {}) or {}
    return int(per_task.get(task, getattr(settings, "AI_QUOTA_USER_DAILY", 0)) or 0)


def buckets_for(task: str, user_id=None, partner_id=None, now=None) -> list[Bucket]:
    """Most specific first, so the user is told about their own cap before the site-wide one."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end, day = day_start + timedelta(days=1), f"{local:%Y%m%d}"
    minute_start = now.replace(second=0, microsecond=0)
    wanted = [
        (user_id is not None, f"u:{user_id}:{task}:{day}", user_daily_limit(task), "user_daily", day_end),
        (partner_id is not None, f"p:{partner_id}:{day}", int(getattr(settings, "AI_QUOTA_PARTNER_DAILY", 0) or 0),
         "partner_daily", day_end),
        (True, f"g:d:{day}", int(getattr(settings, "AI_QUOTA_GLOBAL_PER_DAY", 0) or 0), "global_daily", day_end),
        (True, f"g:m:{minute_start:%Y%m%d%H%M}", int(getattr(settings, "AI_QUOTA_GLOBAL_PER_MINUTE", 0) or 0),
         "global_minute", minute_start + timedelta(minutes=1)),
    ]
    return [Bucket(key[:160], limit, scope, end) for applies, key, limit, scope, end in wanted if applies and limit > 0]


def _take(bucket: Bucket) -> bool:
    from .models import AIQuotaCounter

    if AIQuotaCounter.objects.filter(key=bucket.key, used__lt=bucket.limit).update(used=F("used") + 1):
        return True
    if AIQuotaCounter.objects.filter(key=bucket.key).exists():
        return False  # the window is full
    try:
        with transaction.atomic():
            AIQuotaCounter.objects.create(key=bucket.key, used=1, expires_at=bucket.expires_at)
        return True
    except IntegrityError:  # another request created the row a moment ago
        return bool(AIQuotaCounter.objects.filter(key=bucket.key, used__lt=bucket.limit).update(used=F("used") + 1))


_warned = False
LOCK_RETRIES = 8


def _is_lock_error(exc) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text or "deadlock" in text or "could not serialize" in text


def _db_unavailable(exc):
    global _warned
    if not _warned:
        logger.warning("AI quota/usage bookkeeping skipped: database unavailable (%s)", type(exc).__name__)
        _warned = True


def reserve(task: str, *, user_id=None, partner_id=None) -> Reservation:
    if not enabled():
        return Reservation()
    now = timezone.now()
    buckets = buckets_for(task, user_id, partner_id, now)
    if not buckets:
        return Reservation()
    for attempt in range(LOCK_RETRIES + 1):
        try:
            with transaction.atomic():
                for bucket in buckets:
                    if not _take(bucket):
                        raise QuotaExceeded(bucket.scope, bucket.retry_after(now), bucket.limit)
            break
        except QuotaExceeded as exc:
            logger.info("AI quota refused %s (user %s, partner %s): %s", task, user_id, partner_id, exc)
            raise
        except OperationalError as exc:
            if not _is_lock_error(exc):
                _db_unavailable(exc)  # e.g. the table does not exist yet (before migrate): fail open
                return Reservation()
            if attempt == LOCK_RETRIES:  # heavy write contention: refuse (fail closed) rather than overshoot a cap
                raise QuotaExceeded("busy", 1) from exc
            time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
        except Exception as exc:  # no database at all: fail open
            _db_unavailable(exc)
            return Reservation()
    if random.random() < 0.01:
        purge_expired()
    return Reservation(buckets)


def release(reservation: Reservation) -> None:
    """Give the reserved units back (no provider answered, so nothing was really used)."""
    from .models import AIQuotaCounter

    if not reservation or not reservation.buckets:
        return
    try:
        with transaction.atomic():
            AIQuotaCounter.objects.filter(key__in=[b.key for b in reservation.buckets], used__gt=0).update(used=F("used") - 1)
    except Exception as exc:
        _db_unavailable(exc)


def remaining(task: str, *, user_id=None, partner_id=None) -> dict:
    """{scope: units left} for the caps that apply (for UI hints / admin)."""
    from .models import AIQuotaCounter

    buckets = buckets_for(task, user_id, partner_id)
    used = dict(AIQuotaCounter.objects.filter(key__in=[b.key for b in buckets]).values_list("key", "used"))
    return {b.scope: max(0, b.limit - used.get(b.key, 0)) for b in buckets}


def purge_expired(older_than=timedelta(days=1)) -> int:
    from .models import AIQuotaCounter, AIUsage

    try:
        deleted, _ = AIQuotaCounter.objects.filter(expires_at__lt=timezone.now() - older_than).delete()
        retention = int(getattr(settings, "AI_USAGE_RETENTION_DAYS", 90) or 0)
        if retention:
            AIUsage.objects.filter(created_at__lt=timezone.now() - timedelta(days=retention)).delete()
        return deleted
    except Exception as exc:
        _db_unavailable(exc)
        return 0


def record_usage(task: str, *, user_id=None, partner_id=None, provider="", model="", prompt_tokens=0, completion_tokens=0,
                 total_tokens=0, latency_ms=0, ok=True, cached=False, error="") -> None:
    from .models import AIUsage

    try:
        with transaction.atomic():  # a savepoint: a failed insert never breaks the caller's transaction
            AIUsage.objects.create(user_id=user_id, partner_id=partner_id, task=task[:60], provider=provider[:40],
                                   model=(model or "")[:120], prompt_tokens=max(0, prompt_tokens),
                                   completion_tokens=max(0, completion_tokens), total_tokens=max(0, total_tokens),
                                   latency_ms=max(0, latency_ms), ok=ok, cached=cached, error=(error or "")[:200])
    except Exception as exc:
        _db_unavailable(exc)
