"""Small cache-backed rate limiter for public POST endpoints (login, signup, password reset, uploads).

Fixed window per client IP (and per user when signed in). Uses Django's cache, so no extra dependency;
on PythonAnywhere's single web worker the default local-memory cache is enough.
"""
import time

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from django.urls import Resolver404, resolve

# url name → (max POSTs, window in seconds[, (POST field, value)]). The optional third item limits only the POSTs that
# carry that field (value None: any value), so a page's other buttons (e.g. "Confirm purchase") are not counted.
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
    # Audit L1: promo / discount codes must not be guessable by brute force.
    "course_checkout": (10, 60 * 60, ("apply_discount", None)),
    "startup_upgrade": (10, 60 * 60, ("action", "apply")),
    # AI career features (each POST queues an AI job; the engine also enforces per-user / per-company AI quotas)
    "interview_kit_new": (30, 60 * 60),
    "interview_practice": (30, 60 * 60),
    "interview_kit_enrich": (20, 60 * 60),
    "api_interview_kits": (30, 60 * 60),
    "api_career_advice": (10, 60 * 60),
    "api_resume_polish": (20, 60 * 60),
    # CPU-heavy uploads and AI analysis: each POST parses a document or queues a model call. The AI engine's daily
    # quotas cap spend; these cap the parsing work and request floods per account (or per IP when signed out).
    "skills_hub": (20, 60 * 60),  # CV / LinkedIn upload → analysis
    "skills_api_cv_analysis": (20, 60 * 60),
    "skills_api_skills_extract": (30, 60 * 60),
    "skills_api_assessments": (30, 60 * 60),
    "skills_api_answer_evaluate": (60, 60 * 60),
    "cv_builder_polish": (20, 60 * 60),
    "cv_builder_voice": (10, 60 * 60),  # up to 15 MB of audio + a transcription call each
    "freelance_importer": (20, 60 * 60),
    "freelance_gig_proof": (20, 60 * 60),
}


def _counted(request, only) -> bool:
    if not only:
        return True
    field, value = only[0]
    return field in request.POST if value is None else request.POST.get(field) == value


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
            if name in RULES and _counted(request, RULES[name][2:]):
                limit, window = RULES[name][:2]
                who = f"u{request.user.pk}" if getattr(request, "user", None) and request.user.is_authenticated else client_ip(request)
                key = f"rl:{name}:{who}"
                if not hit(key, limit, window):
                    return _limited(request, key, window)
        return self.get_response(request)


def _seconds_left(key, window) -> int:
    end = cache.get(key + ":end")
    return max(1, min(window, int(end - time.time()) + 1)) if end else window


def _limited(request, key, window):
    """429 with Retry-After: the shared JSON error shape for API clients, the friendly page for browsers."""
    wait = _seconds_left(key, window)
    if request.path_info.startswith("/api/") or "application/json" in request.headers.get("Accept", ""):
        from django.utils.translation import gettext as _

        from .api_responses import json_error

        return json_error(_("Too many requests. Please wait a few minutes and try again."), "rate_limited", 429,
                          retry_after=wait)
    response = render(request, "429.html", {"minutes": max(1, -(-wait // 60))}, status=429)
    response["Retry-After"] = str(wait)
    return response
