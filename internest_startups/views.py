import secrets

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.translation import gettext as _

from internest_core.models import PartnerProfile, StudentProfile
from internest_core.views import get_user_context

from .completion import PLACEHOLDER_PREFIX
from .forms import CompanyIdentityForm, CompanyProfileForm, LogoForm, StartupSignupForm
from .gate import pro_company_required, startup_partner


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
        old_logo, old_name = PartnerProfile.objects.filter(pk=partner.pk).values_list("logo", "company_name").first()
        with transaction.atomic():
            # Write only the fields on this form: never overwrite admin-managed flags such as is_fully_verified.
            company = identity.save(commit=False)
            company.save(update_fields=list(identity.Meta.fields))
            profile = details.save(commit=False)
            profile.partner = partner
            # Social links, website, logo, description: self-service, verification untouched.
            # Company name (core identity) on a verified account: keep access, flag for admin re-check.
            if partner.is_fully_verified and existing is not None and old_name != partner.company_name:
                profile.identity_changed_at = timezone.now()
                profile.previous_company_name = old_name
            profile.save()
            partner.calculate_completion()
        _delete_replaced_logo(old_logo, partner)
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
    from .pricing import PERIOD_MONTHS, currency_for, format_price, gateway_for, pricing_country
    from internest_lounge.moderation import POSTS_PER_HOUR

    from .talent import INVITES_PER_OPPORTUNITY
    from .tiers import FREE_POSTS_PER_MONTH, is_pro, quote

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    country = pricing_country(request, partner)
    currency = currency_for(country)
    period = request.POST.get("period") or request.GET.get("period")
    period = period if period in PERIOD_MONTHS else "month"
    back = reverse("startup_upgrade") + ("?period=year" if period == "year" else "")

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
                q = quote(promo, period, currency)
                ProUpgradeRequest.objects.create(
                    partner=partner, months=q["months"], list_price=q["list_price"], promo_code=promo,
                    discount_percent=q["discount_percent"], final_price=q["final_price"],
                    currency=currency, country=country, gateway=gateway_for(currency),
                )
                request.session.pop(PROMO_SESSION_KEY, None)
                messages.success(request, _("Request received! Our team will contact you within 24 hours to complete payment and activate Pro."))
        return redirect(back)

    promo = _session_promo(request)
    context = get_user_context(request)
    context.update({
        "partner": partner,
        "promo": promo,
        "quote": quote(promo, period, currency),
        "monthly": quote(None, "month", currency), "yearly": quote(None, "year", currency),
        "free_price": format_price(0, currency), "currency": currency, "period": period,
        "invites_per_opportunity": INVITES_PER_OPPORTUNITY, "network_hourly": POSTS_PER_HOUR,
        "is_pro": is_pro(partner),
        "subscription": getattr(partner, "subscription", None),
        "pending_request": ProUpgradeRequest.objects.filter(partner=partner, status=ProUpgradeRequest.STATUS_PENDING).first(),
        "free_posts": FREE_POSTS_PER_MONTH,
    })
    return render(request, "startups/upgrade.html", context)


def _delete_replaced_logo(old_name, partner):
    if old_name and old_name != partner.logo.name:
        storage = partner._meta.get_field("logo").storage
        if storage.exists(old_name):
            storage.delete(old_name)


@login_required
@require_POST
def company_logo_update(request):
    """Instant, self-service logo change. Verification status and other profile data stay untouched."""
    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    old_logo = partner.logo.name
    form = LogoForm(request.POST, request.FILES, instance=partner)
    if form.is_valid():
        partner.logo = form.cleaned_data["logo"]
        partner.save(update_fields=["logo"])  # stores the file; writes only the logo column
        _delete_replaced_logo(old_logo, partner)
        messages.success(request, _("Logo updated."))
    else:
        errors = [e for errs in form.errors.values() for e in errs]
        messages.error(request, errors[0] if errors else _("Could not update the logo. Please try another image."))
    nxt = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = reverse("startup_company_profile")
    return redirect(nxt)


@pro_company_required
def talent_pool_search_view(request):
    from django.core.paginator import Paginator

    from internest_core.models import Internship

    from django.db.models import Count

    from .talent import DAILY_INVITATIONS, INVITES_PER_OPPORTUNITY, TOP_SCORE, filter_options, invitations_today, pool_queryset

    partner = request.partner
    page = Paginator(pool_queryset(request.GET), 24).get_page(request.GET.get("page"))
    invited = set(partner.talent_invitations.filter(student__in=list(page)).values_list("student_id", flat=True))
    params = request.GET.copy()
    params.pop("page", None)
    opportunities = list(
        Internship.objects.filter(partner=partner, is_active=True, deadline__gte=timezone.now().date())
        .annotate(invites_used=Count("talent_invitations")).order_by("-pk")
    )
    # What the company can really still send: per-opportunity quota left, capped by the daily anti-spam limit.
    per_post_left = sum(max(INVITES_PER_OPPORTUNITY - o.invites_used, 0) for o in opportunities)
    invites_left = min(per_post_left, max(DAILY_INVITATIONS - invitations_today(partner), 0))
    context = get_user_context(request)
    context.update({
        "page": page, "invited": invited, "filters": request.GET, "querystring": params.urlencode(),
        "options": filter_options(),
        "opportunities": opportunities,
        "invites_per_opportunity": INVITES_PER_OPPORTUNITY,
        "score_steps": [n for n in (TOP_SCORE, 80, 90, 95) if n >= TOP_SCORE],
        "invites_left": invites_left,
        "invites_daily": DAILY_INVITATIONS,
    })
    return render(request, "startups/talent_pool.html", context)


@pro_company_required
@require_POST
def talent_pool_invite(request, student_id):
    from django.db import IntegrityError
    from django.shortcuts import get_object_or_404

    from internest_core.models import Internship

    from .models import TalentInvitation
    from .talent import DAILY_INVITATIONS, INVITES_PER_OPPORTUNITY, invitations_today, qualifying_skills, send_invitation_email

    partner = request.partner
    back = reverse("talent_pool")
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}) and next_url.startswith(back):
        back = next_url
    student = get_object_or_404(
        StudentProfile.objects.select_related("user"),
        pk=student_id, talent_pool_visible=True, pk__in=qualifying_skills().values("student_id"),
    )
    internship = Internship.objects.filter(
        pk=request.POST.get("internship") or 0, partner=partner, is_active=True, deadline__gte=timezone.now().date(),
    ).first()
    if internship is None:
        messages.error(request, _("Choose one of your open opportunities."))
    elif internship.talent_invitations.count() >= INVITES_PER_OPPORTUNITY:
        messages.error(request, _("Each opportunity can have at most %(n)s invitations.") % {"n": INVITES_PER_OPPORTUNITY})
    elif invitations_today(partner) >= DAILY_INVITATIONS:
        messages.error(request, _("You've reached today's limit of %(n)s invitations. Try again tomorrow.") % {"n": DAILY_INVITATIONS})
    else:
        try:
            with transaction.atomic():
                invitation = TalentInvitation.objects.create(
                    partner=partner, student=student, internship=internship, message=request.POST.get("message", "").strip()[:500],
                )
        except IntegrityError:
            messages.info(request, _("This student was already invited to this opportunity."))
        else:
            send_invitation_email(invitation, request.build_absolute_uri(invitation.apply_url))
            messages.success(request, _("Invitation sent. Contact details unlock once the student applies and you shortlist them."))
    return redirect(back)


@pro_company_required
def talent_pool_candidate(request, student_id):
    """Profile preview (modal fragment): studies, bio, portfolio and verified skills — never contact details."""
    from django.shortcuts import get_object_or_404

    from .talent import qualifying_skills

    candidate = get_object_or_404(
        StudentProfile.objects.select_related("user"),
        pk=student_id, talent_pool_visible=True, pk__in=qualifying_skills().values("student_id"),
    )
    return render(request, "startups/_candidate_preview.html", {
        "c": candidate,
        "skills": qualifying_skills().filter(student=candidate).select_related("skill").order_by("-score", "skill__name"),
        "invited": request.partner.talent_invitations.filter(student=candidate).select_related("internship"),
    })
