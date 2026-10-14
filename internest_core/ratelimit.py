"""Small cache-backed rate limiter for public POST endpoints (login, signup, password reset, uploads).

Fixed window per client IP (and per user when signed in). Uses Django's cache, so no extra dependency;
on PythonAnywhere's single web worker the default local-memory cache is enough.
"""
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from django.urls import Resolver404, resolve

# url name → (max POSTs, window in seconds)
RULES = {
    "login": (10, 15 * 60),
    "signup": (5, 60 * 60),
    "password_reset": (5, 60 * 60),
    "resend_verification": (5, 60 * 60),
    "certificate_submit": (10, 60 * 60),
}


def client_ip(request):
    """The proxy-set client address (PythonAnywhere sends X-Real-IP); X-Forwarded-For is client-controlled."""
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "") or "unknown"


def hit(key, limit, window):
    """Count one request against `key`; False when the limit for this window is already used up."""
    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, window)
        count = 1
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
