import secrets

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from internest_core.models import PartnerProfile
from internest_core.views import get_user_context

from .completion import PLACEHOLDER_PREFIX
from .forms import CompanyIdentityForm, CompanyProfileForm, StartupSignupForm
from .gate import startup_partner


def startup_register(request):
    if request.user.is_authenticated:
        return redirect("home_redirect")
    form = StartupSignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        first, _sep, last = data["full_name"].strip().partition(" ")
        with transaction.atomic():
            user = User.objects.create_user(
                username=data["email"], email=data["email"], password=data["password1"],
                first_name=first[:150], last_name=last[:150],
            )
            # Placeholder name until the company profile step; partner_code is required by the model but unused in the UI.
            PartnerProfile.objects.create(
                user=user,
                company_name=f"{PLACEHOLDER_PREFIX}{secrets.token_hex(6)}",
                partner_code=secrets.token_urlsafe(9),
            )
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, _("Account created! Tell us about your company to continue."))
        return redirect("startup_company_profile_edit")
    context = get_user_context(request)
    context["form"] = form
    return render(request, "startups/register.html", context)


def _startup_or_redirect(request):
    partner = startup_partner(request.user)
    if partner is None:
        messages.error(request, _("This page is for startup accounts."))
    return partner


@login_required
def company_profile(request):
    """Read-only showcase card; the form lives on the edit page."""
    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    profile = getattr(partner, "company_profile", None)
    if profile is None:
        return redirect("startup_company_profile_edit")
    socials = [(label, icon, url) for label, icon, url in (
        ("LinkedIn", "bi-linkedin", partner.linkedin_url),
        ("Facebook", "bi-facebook", partner.facebook_url),
        ("Twitter", "bi-twitter-x", partner.twitter_url),
        ("Instagram", "bi-instagram", partner.instagram_url),
    ) if url]
    context = get_user_context(request)
    context.update({
        "partner": partner, "profile": profile, "socials": socials,
        "company_age": max(0, timezone.now().year - profile.founded_year),
    })
    return render(request, "startups/company_showcase.html", context)


@login_required
def company_profile_edit(request):
    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    existing = getattr(partner, "company_profile", None)
    identity = CompanyIdentityForm(request.POST or None, request.FILES or None, instance=partner)
    details = CompanyProfileForm(request.POST or None, instance=existing, company_email=request.POST.get("official_email"))
    if request.method == "POST" and identity.is_valid() and details.is_valid():
        with transaction.atomic():
            identity.save()
            profile = details.save(commit=False)
            profile.partner = partner
            profile.save()
            partner.calculate_completion()
        if existing is None and not partner.is_fully_verified:
            messages.success(request, _("Thanks! Your company profile was submitted for verification."))
        else:
            messages.success(request, _("Company profile saved."))
        return redirect("startup_company_profile")
    context = get_user_context(request)
    context.update({"identity": identity, "details": details, "is_new": existing is None, "partner": partner})
    return render(request, "startups/company_profile.html", context)
