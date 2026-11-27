"""Who may open /founder/: active staff accounts holding `internest_founder.access_control_center`
(superusers hold every permission). Enforced on the server for every view and JSON endpoint; templates never decide.

    @founder_required        anonymous -> login redirect, signed-in non-founder -> 403 (nothing about the page leaks)
    audit(request, op, target, ok=True, **detail)   one SecurityEvent per sensitive founder action (ids only)
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.views.decorators.cache import never_cache

from internest_core import audit as core_audit

PERMISSION = "internest_founder.access_control_center"


def is_founder(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_staff and user.has_perm(PERMISSION))


def founder_required(view):
    @wraps(view)
    @never_cache
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_founder(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def audit(request, op: str, target="", ok: bool = True, **detail):
    from internest_core.models import SecurityEvent

    core_audit.record(request, SecurityEvent.FOUNDER_ACTION, target, op=op, ok=ok, **detail)
