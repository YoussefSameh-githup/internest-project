"""Per-provider circuit breaker in the Django cache (shared by every worker when the cache is Redis / file based).

    closed     normal; AI_BREAKER_FAILURES consecutive failures within AI_BREAKER_WINDOW_SECONDS open it
    open       the provider is skipped for AI_BREAKER_OPEN_SECONDS (or the provider's own 429 Retry-After, capped)
    half_open  the open period is over: exactly one request probes it; success closes, failure re-opens

Without it, every request during an outage walks the whole chain and waits for each dead provider's timeout.
State is advisory (a lost cache only means one extra probe), so the plain get/set races of a file cache are fine;
the half-open probe uses cache.add, which is atomic on Redis and local memory.
"""
import logging
import time
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
HALF_OPEN_GRACE = 600  # seconds the "was open" marker survives after the open period (then the state is plain closed)


def _now() -> float:
    return time.time()


def _setting(name, default):
    return getattr(settings, name, default)


def enabled() -> bool:
    return bool(_setting("AI_BREAKER_ENABLED", True))


def _key(provider: str, part: str) -> str:
    return f"ai:breaker:{provider}:{part}"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # a broken cache must never break the AI call
        logger.exception("AI circuit breaker cache error")
        return default


def state(provider: str) -> str:
    if not enabled():
        return CLOSED
    until = _safe(lambda: cache.get(_key(provider, "open_until")))
    if until is None:
        return CLOSED
    return OPEN if _now() < until else HALF_OPEN


def available(provider: str) -> bool:
    """May the router put this provider in the chain? (Does not consume the half-open probe.)"""
    current = state(provider)
    if current == CLOSED:
        return True
    if current == OPEN:
        return False
    return _safe(lambda: cache.get(_key(provider, "probe")) is None, True)


def allow(provider: str) -> bool:
    """Right before calling: True when closed, False when open, and True for the ONE half-open probe."""
    current = state(provider)
    if current == CLOSED:
        return True
    if current == OPEN:
        return False
    return _safe(lambda: cache.add(_key(provider, "probe"), 1, int(_setting("AI_BREAKER_OPEN_SECONDS", 60)) or 60), True)


def record_success(provider: str) -> None:
    if enabled():
        if state(provider) != CLOSED:
            logger.info("AI circuit for %s closed again (probe succeeded)", provider)
        _safe(lambda: cache.delete_many([_key(provider, p) for p in ("fails", "open_until", "probe")]))


def retry_after_of(exc) -> int | None:
    """Seconds from a 429 reply's Retry-After header (delta-seconds or HTTP date), else None."""
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", getattr(exc, "status_code", None)) != 429:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - _now()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return max(1, min(int(seconds + 0.999), int(_setting("AI_BREAKER_MAX_RETRY_AFTER", 900))))


def _open(provider: str, seconds: int, why: str) -> None:
    until = _now() + seconds
    _safe(lambda: cache.set(_key(provider, "open_until"), until, seconds + HALF_OPEN_GRACE))
    _safe(lambda: cache.delete_many([_key(provider, "fails"), _key(provider, "probe")]))
    logger.warning("AI circuit for %s opened for %ss (%s)", provider, seconds, why)


def record_failure(provider: str, exc=None) -> None:
    if not enabled():
        return
    retry_after = retry_after_of(exc) if exc is not None else None
    if retry_after:
        _open(provider, retry_after, "429 Retry-After")
        return
    open_seconds = int(_setting("AI_BREAKER_OPEN_SECONDS", 60))
    if state(provider) == HALF_OPEN:
        _open(provider, open_seconds, "half-open probe failed")
        return
    window = int(_setting("AI_BREAKER_WINDOW_SECONDS", 60))
    now = _now()
    count, started = _safe(lambda: cache.get(_key(provider, "fails")), None) or (0, now)
    if now - started > window:
        count, started = 0, now
    count += 1
    if count >= int(_setting("AI_BREAKER_FAILURES", 3)):
        _open(provider, open_seconds, f"{count} consecutive failures")
    else:
        _safe(lambda: cache.set(_key(provider, "fails"), (count, started), window))


def reset(provider: str | None = None) -> None:
    """Close one provider's circuit (or, with no argument, every circuit this process knows about via the providers)."""
    from .providers import configured

    keys = [provider] if provider else [p.key for p in configured()]
    for key in keys:
        _safe(lambda k=key: cache.delete_many([_key(k, p) for p in ("fails", "open_until", "probe")]))
