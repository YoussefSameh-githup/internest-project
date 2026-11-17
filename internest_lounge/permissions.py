from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext as _

from internest_core import identity


def lounge_member(user):
    """Verified startup/company partner (not universities, not students), else None."""
    partner = identity.partner_profile(user)
    if partner is None or partner.is_academic or not partner.is_fully_verified:
        return None
    return partner


def founders_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        partner = lounge_member(request.user)
        if partner is None:
            messages.error(request, _("This space is strictly reserved for verified startup founders."))
            return redirect("landing")
        request.founder = partner
        return view(request, *args, **kwargs)
    return wrapper
