"""Entry point for the "Continue with ..." buttons.

The button does not link straight to allauth: this view first records which flow the visitor came from (student or partner)
in the server-side session, validates `next`, and only then sends them to the provider. allauth enforces the OAuth `state`.

POST only, behind Django's CSRF check: a GET link that starts an OAuth flow lets another site log a visitor into an
account of the attacker's choosing (login CSRF). SOCIALACCOUNT_LOGIN_ON_GET is off for the same reason.
"""
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.helpers import render_authentication_error
from allauth.socialaccount.providers.oauth2.client import OAuth2Error
from django.core.exceptions import PermissionDenied
from requests import RequestException
from django.http import Http404
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .adapters import ROLES, remember_role
from .context_processors import configured_social_providers


@require_POST
def social_start(request, role, provider):
    if role not in ROLES or provider not in {p["id"] for p in configured_social_providers()}:
        raise Http404("Unknown or unconfigured sign-in method")
    if request.user.is_authenticated:
        return redirect("home_redirect")
    target = request.POST.get("next", "")
    if not target or not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = ""
    remember_role(request, role)
    social_provider = get_adapter(request).get_provider(request, provider)
    try:
        return social_provider.redirect(request, "login", next_url=target or None)
    except (OAuth2Error, RequestException, PermissionDenied) as exc:  # same handling as allauth's own login view
        return render_authentication_error(request, social_provider, exception=exc)


class SocialProviderGuardMiddleware:
    """allauth registers /oauth/<provider>/login/ and /callback/ for every installed provider, configured or not; a
    request for one without credentials raised SocialApp.DoesNotExist (an anonymous, unlimited 500). Providers that
    are not offered (no keys, or https-only while the site runs on plain http) now answer 404, the same as their
    hidden buttons and social_start."""

    def __init__(self, get_response):
        from django.conf import settings

        self.get_response = get_response
        self.prefixes = {f"/oauth/{p}/": p for p in settings.SOCIAL_LOGIN_PROVIDER_ORDER}

    def __call__(self, request):
        path = request.path_info
        if path.startswith("/oauth/"):
            for prefix, provider in self.prefixes.items():
                if path.startswith(prefix) and provider not in {p["id"] for p in configured_social_providers()}:
                    raise Http404("Unknown or unconfigured sign-in method")
        return self.get_response(request)
