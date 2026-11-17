"""Deterministic cache for idempotent AI requests (temperature 0): the same prompt never pays twice.

    value = ai_cache.cached("chat_json", {"system": s, "user": u, "models": [...]}, lambda: call_the_api())

Keys are SHA-256 of the canonical JSON payload, stored in Django's cache (Redis / file / local memory, see settings.CACHES)
for settings.AI_CACHE_TTL seconds (0 disables). Failures are never cached: only a value `compute()` returned is stored.
Use `with no_ai_cache():` for calls that must stay fresh (e.g. quiz generation: every attempt needs new questions).
"""
import contextlib
import contextvars
import hashlib
import json
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)
VERSION = 1
_disabled = contextvars.ContextVar("ai_cache_disabled", default=False)


def cache_key(namespace: str, payload) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"ai:{namespace}:v{VERSION}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def ttl() -> int:
    return int(getattr(settings, "AI_CACHE_TTL", 0) or 0)


def enabled() -> bool:
    return ttl() > 0 and not _disabled.get()


@contextlib.contextmanager
def no_ai_cache():
    token = _disabled.set(True)
    try:
        yield
    finally:
        _disabled.reset(token)


def cached(namespace: str, payload, compute):
    """Return the cached answer for `payload`, or compute, store and return it."""
    if not enabled():
        return compute()
    key = cache_key(namespace, payload)
    try:
        hit = cache.get(key)
    except Exception:  # a broken cache backend must never break the AI feature
        logger.exception("AI cache read failed")
        hit = None
    if hit is not None:
        logger.debug("AI cache hit %s", key)
        return hit
    value = compute()
    if value is not None:
        try:
            cache.set(key, value, ttl())
        except Exception:
            logger.exception("AI cache write failed")
    return value
