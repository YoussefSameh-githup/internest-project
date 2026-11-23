"""Site-wide Content-Security-Policy with a per-response script nonce.

Every request gets a fresh 128-bit nonce (`request.csp_nonce`, `{{ csp_nonce }}` in templates). The header allows
scripts from our own origin, https: CDNs (Bootstrap, Chart.js) and inline `<script nonce="…">` blocks that carry
that response's nonce. There is NO 'unsafe-inline' for scripts, so injected markup cannot run script, and inline
event handlers (onclick=…) are refused too: templates use data-* hooks wired in static/js/ui_behaviors.js instead.

Styles keep 'unsafe-inline' (hundreds of style="" attributes; CSS cannot run script). The Django admin (django-unfold,
Alpine.js) keeps its own policy with 'unsafe-inline' and 'unsafe-eval': it is staff-only and its templates are not
ours to nonce. Responses that already carry a stricter policy (sandboxed /media/ files) keep theirs.

Cached public pages store a placeholder in place of the nonce and get a fresh one on every hit
(internest_core/page_cache.py), so a nonce is never reused across responses.
"""
import secrets

from django.conf import settings

BASE = {
    "default-src": "'self'",
    "script-src": "'self' {nonce} https:",
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


def new_nonce() -> str:
    return secrets.token_urlsafe(16)  # 128 bits; URL-safe base64 is valid in a CSP nonce source


def build_policy(path: str, nonce: str = "") -> str:
    rules = dict(BASE)
    if path.startswith("/admin/"):
        rules.update(ADMIN_EXTRA)
    rules["script-src"] = rules["script-src"].replace("{nonce}", f"'nonce-{nonce}'" if nonce else "").replace("  ", " ")
    return "; ".join(f"{k} {v}" for k, v in rules.items())


# Browser features the site may use. Only the microphone (Voice-to-CV, after the student presses record) is allowed,
# and only for our own pages; camera, location and the rest are switched off for us and anything we embed.
PERMISSIONS_POLICY = ", ".join([
    "camera=()", "microphone=(self)", "geolocation=()", "payment=()", "usb=()", "serial=()", "bluetooth=()",
    "magnetometer=()", "gyroscope=()", "accelerometer=()", "display-capture=()", "browsing-topics=()",
])

ADMIN_POLICY = build_policy("/admin/")


class ContentSecurityPolicyMiddleware:
    """Runs near the top of MIDDLEWARE so every response, early 4xx pages included, gets a nonce and a policy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = new_nonce()
        response = self.get_response(request)
        if getattr(settings, "CSP_ENABLED", True) and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = (
                ADMIN_POLICY if request.path.startswith("/admin/") else build_policy(request.path, request.csp_nonce))
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = PERMISSIONS_POLICY
        return response


def nonce_context(request):
    """Template context processor: {{ csp_nonce }}."""
    return {"csp_nonce": getattr(request, "csp_nonce", "")}
