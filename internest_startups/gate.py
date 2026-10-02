"""Startup onboarding gates: mandatory company profile, and verification before posting."""
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.urls import Resolver404, resolve

from internest_core.models import PartnerProfile

# URL names a startup without a company profile may still reach.
PROFILE_GATE_ALLOWED = frozenset({
    "startup_company_profile", "logout", "set_language", "serve_media", "landing", "startup_register",
})
PROFILE_GATE_PREFIXES = ("/admin/", "/static/", "/i18n/", "/accounts/")


def startup_partner(user):
    """Non-academic partner account (startups/companies), else None."""
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        partner = user.partnerprofile
    except PartnerProfile.DoesNotExist:
        return None
    return None if partner.is_academic else partner


def needs_company_profile(partner) -> bool:
    return partner is not None and not hasattr(partner, "company_profile")


def posting_block_reason(partner):
    """Publish gate. Startups need BOTH (A) admin verification and (B) a 100% complete profile.
    Returns a user-facing error, or None when posting is allowed."""
    if partner is None:
        return _("You are not registered as a partner.")
    if partner.is_academic:  # universities keep their existing flow
        return None if partner.profile_completion_score >= 100 else _(
            "Your partner profile must be 100% complete to post opportunities.")
    if not partner.is_fully_verified:
        return _("Your startup is under review. You can post opportunities once an admin verifies it (within 24 hours).")
    if partner.profile_completion_score < 100:
        return _("Your profile is %(score)s%% complete. Complete it to 100%% to post opportunities.") % {
            "score": partner.profile_completion_score}
    return None


def can_post_opportunities(partner) -> bool:
    return posting_block_reason(partner) is None


class CompanyProfileGateMiddleware:
    """Startups must complete their company profile before using any other page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        partner = startup_partner(request.user)
        if needs_company_profile(partner) and not self._allowed(request.path_info):
            return redirect("startup_company_profile")
        return self.get_response(request)

    @staticmethod
    def _allowed(path):
        if path.startswith(PROFILE_GATE_PREFIXES):
            return True
        try:
            return resolve(path).url_name in PROFILE_GATE_ALLOWED
        except Resolver404:
            return False
