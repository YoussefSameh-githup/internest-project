"""Entry point for the "Continue with ..." buttons.

The button does not link straight to allauth: this view first records which flow the visitor came from (student or partner)
in the server-side session, validates `next`, and only then sends them to the provider. allauth enforces the OAuth `state`.
"""
from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

from .adapters import ROLES, remember_role
from .context_processors import configured_social_providers


@require_GET
def social_start(request, role, provider):
    if role not in ROLES or provider not in {p["id"] for p in configured_social_providers()}:
        raise Http404("Unknown or unconfigured sign-in method")
    if request.user.is_authenticated:
        return redirect("home_redirect")
    target = request.GET.get("next", "")
    if not target or not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = ""
    remember_role(request, role)
    query = {"process": "login", **({"next": target} if target else {})}
    return redirect(f"{reverse(f'{provider}_login')}?{urlencode(query)}")
