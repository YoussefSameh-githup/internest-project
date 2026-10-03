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
    socials = [(key, label, url) for key, label, url in (
        ("linkedin", "LinkedIn", partner.linkedin_url),
        ("facebook", "Facebook", partner.facebook_url),
        ("twitter", "X / Twitter", partner.twitter_url),
        ("instagram", "Instagram", partner.instagram_url),
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


PROMO_SESSION_KEY = "pro_promo_code"


def _session_promo(request):
    from .models import PromoCode

    code = request.session.get(PROMO_SESSION_KEY)
    promo = PromoCode.objects.filter(code__iexact=code).first() if code else None
    if promo is not None and not promo.is_valid():
        request.session.pop(PROMO_SESSION_KEY, None)
        promo = None
    return promo


@login_required
def upgrade(request):
    from .models import PromoCode, ProUpgradeRequest
    from .tiers import FREE_POSTS_PER_MONTH, is_pro, quote

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "apply":
            code = request.POST.get("promo_code", "").strip()
            promo = PromoCode.objects.filter(code__iexact=code).first()
            if promo is None or not promo.is_valid():
                messages.error(request, _("This promo code is invalid or has expired."))
            else:
                request.session[PROMO_SESSION_KEY] = promo.code
                messages.success(request, _("Promo code applied: %(percent)s%% off.") % {"percent": promo.discount_percent})
        elif action == "remove":
            request.session.pop(PROMO_SESSION_KEY, None)
        elif action == "subscribe":
            if ProUpgradeRequest.objects.filter(partner=partner, status=ProUpgradeRequest.STATUS_PENDING).exists():
                messages.info(request, _("You already have a Pro request awaiting payment. Our team will contact you shortly."))
            else:
                promo = _session_promo(request)
                q = quote(promo)
                ProUpgradeRequest.objects.create(
                    partner=partner, list_price=q["list_price"], promo_code=promo,
                    discount_percent=q["discount_percent"], final_price=q["final_price"],
                )
                request.session.pop(PROMO_SESSION_KEY, None)
                messages.success(request, _("Request received! Our team will contact you within 24 hours to complete payment and activate Pro."))
        return redirect("startup_upgrade")

    promo = _session_promo(request)
    context = get_user_context(request)
    context.update({
        "partner": partner,
        "promo": promo,
        "quote": quote(promo),
        "is_pro": is_pro(partner),
        "subscription": getattr(partner, "subscription", None),
        "pending_request": ProUpgradeRequest.objects.filter(partner=partner, status=ProUpgradeRequest.STATUS_PENDING).first(),
        "free_posts": FREE_POSTS_PER_MONTH,
    })
    return render(request, "startups/upgrade.html", context)
