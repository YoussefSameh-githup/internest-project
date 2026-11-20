"""Small cache-backed rate limiter for public POST endpoints (login, signup, password reset, uploads).

Fixed window per client IP (and per user when signed in). Uses Django's cache, so no extra dependency;
on PythonAnywhere's single web worker the default local-memory cache is enough.
"""
import time

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from django.urls import Resolver404, resolve

# url name → (max POSTs, window in seconds)
RULES = {
    "login": (10, 15 * 60),
    "signup": (5, 60 * 60),
    "password_reset": (5, 60 * 60),  # ask for a code (per IP; the service also limits per email: 5/hour, 60 s apart)
    "password_reset_resend": (5, 60 * 60),
    "password_reset_verify": (20, 15 * 60),  # 5 tries per code are enforced in the service; this caps code cycling
    "password_reset_new": (10, 60 * 60),
    "resend_verification": (5, 60 * 60),
    "certificate_submit": (10, 60 * 60),
    "checkout": (10, 60 * 60),  # payment receipt uploads
    "social_start": (20, 15 * 60),  # "Continue with Google / LinkedIn / GitHub / Microsoft" clicks
    # Phase 7: every other public or abuse-prone form
    "verify_email": (15, 15 * 60),  # 6-digit codes: 5 tries per code is enforced in the view, this caps code cycling
    "startup_register": (5, 60 * 60),
    "contact": (5, 60 * 60),
    "university_inquiry": (5, 60 * 60),
    "contact_partner": (10, 60 * 60),
    "apply": (30, 60 * 60),
    "skills_challenge_start": (20, 60 * 60),
    "skills_challenge_start_for_skill": (20, 60 * 60),
    "screening_submit": (20, 60 * 60),
    "founding_claim": (5, 60 * 60),
    "account_export": (5, 60 * 60),
    "account_delete": (5, 60 * 60),
}


def client_ip(request):
    """The proxy-set client address (PythonAnywhere sends X-Real-IP); X-Forwarded-For is client-controlled."""
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "") or "unknown"


def hit(key, limit, window):
    """Count one request against `key`; False when the limit for this window is already used up.

    The value carries its own window end and every write keeps the remaining time: Django's file and database caches
    implement incr() as get + set with the *default* timeout (300 s), which silently turned hourly limits into
    "5 minutes since the last request" (password-reset review)."""
    now = time.time()
    end_key = key + ":end"
    end = cache.get(end_key)
    if end is None or end <= now:  # a new window
        end = now + window
        cache.set(end_key, end, window)
        cache.set(key, 0, window)
    remaining = max(1, int(end - now) + 1)
    try:
        count = cache.incr(key)  # atomic on Redis and local memory
    except ValueError:  # expired between the reads
        cache.set(key, 1, remaining)
        count = 1
    cache.touch(key, remaining)  # file/db caches' incr() rewrites the default timeout: put the window back
    return count <= limit


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and settings.RATELIMIT_ENABLED:
            try:
                name = resolve(request.path_info).url_name
            except Resolver404:
                name = None
            if name in RULES:
                limit, window = RULES[name]
                who = f"u{request.user.pk}" if getattr(request, "user", None) and request.user.is_authenticated else client_ip(request)
                if not hit(f"rl:{name}:{who}", limit, window):
                    return render(request, "429.html", {"minutes": max(1, window // 60)}, status=429)
        return self.get_response(request)
