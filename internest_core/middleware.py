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
        from .models import StudentProfile  # local import: avoid AppRegistryNotReady
        try:
            profile = request.user.studentprofile
        except StudentProfile.DoesNotExist:
            return False
        return profile.has_any_pending_email_verification
