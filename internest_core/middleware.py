"""Middleware that locks unverified students on the verify-email page."""
from django.shortcuts import redirect
from django.urls import resolve, Resolver404


class EmailVerificationGateMiddleware:
    """
    Any authenticated student with an unverified personal OR university email
    may only reach the verify-email page (and a tiny allow-list). Every other
    URL is redirected straight back to /accounts/verify-email/.
    """

    ALLOWED_URL_NAMES = frozenset({
        "verify_email",
        "resend_verification",
        "logout",
        "set_language",
        "serve_media",
        "verify_experience",  # public letter authenticity check
        "experience_letter",
    })
    ALLOWED_PREFIXES = ("/admin/", "/static/", "/i18n/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and self._needs_verification(request):
            if not self._is_allowed(request):
                return redirect("verify_email")
        return self.get_response(request)

    def _is_allowed(self, request) -> bool:
        path = request.path_info
        if any(path.startswith(p) for p in self.ALLOWED_PREFIXES):
            return True
        try:
            match = resolve(path)
        except Resolver404:
            return False
        return match.url_name in self.ALLOWED_URL_NAMES

    def _needs_verification(self, request) -> bool:
        from .identity import student_profile  # local import: avoid AppRegistryNotReady

        profile = student_profile(request.user)
        return profile is not None and profile.has_any_pending_email_verification


class SlidingSessionMiddleware:
    """Keep the 30-day login sliding without a database write on every request.

    With SESSION_SAVE_EVERY_REQUEST=False a session is only saved when it changes. Touching one key at most once per
    SESSION_REFRESH_INTERVAL (a day) re-saves it, which renews both the stored expiry and the cookie max-age.
    Only signed-in sessions are touched, so anonymous visitors never get a session row."""

    KEY = "_refreshed_at"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        session = getattr(request, "session", None)
        if session is not None and session.session_key and "_auth_user_id" in session:
            import time

            from django.conf import settings

            now = int(time.time())
            if now - int(session.get(self.KEY, 0) or 0) >= getattr(settings, "SESSION_REFRESH_INTERVAL", 86400):
                session[self.KEY] = now
        return response


from django.middleware.gzip import GZipMiddleware  # noqa: E402

COMPRESSIBLE_TYPES = ("text/", "application/json", "application/javascript", "application/xml", "image/svg+xml",
                      "application/manifest+json")


class TextGZipMiddleware(GZipMiddleware):
    """GZip for HTML / JSON / text only. PDFs, images and uploads are already compressed: gzipping them again only
    burns CPU. Django's GZipMiddleware already adds random padding against BREACH."""

    def process_response(self, request, response):
        content_type = response.get("Content-Type", "").split(";")[0].strip().lower()
        if not content_type.startswith(COMPRESSIBLE_TYPES):
            return response
        return super().process_response(request, response)
