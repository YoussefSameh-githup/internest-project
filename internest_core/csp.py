"""Site-wide Content-Security-Policy that keeps today's inline scripts and CDN assets working.

Pragmatic first step: 'unsafe-inline' scripts and https: sources stay allowed (templates embed their own JS/CSS),
while plugins, <base> hijacking, framing by other sites and form posts to plain-http hosts are blocked.
Responses that already carry a stricter policy (e.g. sandboxed /media/ files) keep theirs.
"""
from django.conf import settings

BASE = {
    "default-src": "'self'",
    "script-src": "'self' 'unsafe-inline' https:",
    "style-src": "'self' 'unsafe-inline' https:",
    "img-src": "'self' data: blob: https:",
    "font-src": "'self' https: data:",
    "media-src": "'self' blob:",           # Voice-to-CV playback
    "connect-src": "'self'",
    "frame-src": "'self' https:",          # map previews on opportunities
    "object-src": "'none'",
    "base-uri": "'self'",
    "frame-ancestors": "'none'",
    "form-action": "'self' https:",        # checkout redirects to the payment gateway
}
# django-unfold's admin uses Alpine.js, whose standard build evaluates expressions with Function().
ADMIN_EXTRA = {"script-src": "'self' 'unsafe-inline' 'unsafe-eval' https:"}


def build_policy(path: str) -> str:
    rules = dict(BASE)
    if path.startswith("/admin/"):
        rules.update(ADMIN_EXTRA)
    return "; ".join(f"{k} {v}" for k, v in rules.items())


POLICY = build_policy("/")
ADMIN_POLICY = build_policy("/admin/")


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(settings, "CSP_ENABLED", True) and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = ADMIN_POLICY if request.path.startswith("/admin/") else POLICY
        return response
