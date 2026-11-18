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

from internest_core.models import PartnerProfile, StudentProfile, proof_of_work
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
    """Plans: Free (EGP 0) and the Hiring Pass (EGP 500, one opportunity, 30 days, one-time, no renewal), plus Founding 20."""
    from django.conf import settings

    from internest_lounge.moderation import POSTS_PER_HOUR
    from internest_skills.verification import pool_stats

    from . import entitlements
    from .forms import FoundingClaimForm
    from .models import FoundingSeat, PromoCode, ProUpgradeRequest
    from .pricing import FREE_PRICE, currency_for_partner, format_price, pass_quote
    from .talent import INVITES_PER_OPPORTUNITY
    from .tiers import FREE_POSTS_PER_MONTH

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    currency = currency_for_partner(request, partner)

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
        return redirect("startup_upgrade")

    promo = _session_promo(request)
    seat = FoundingSeat.objects.filter(partner=partner).select_related("hiring_pass__internship").first()
    opportunities = entitlements.eligible_opportunities(partner)
    context = get_user_context(request)
    context.update({
        "partner": partner,
        "promo": promo,
        "quote": pass_quote(currency, promo),
        "list_quote": pass_quote(currency),
        "free_price": format_price(FREE_PRICE, "EGP"), "currency": currency,  # Free is EGP 0 for everyone
        "pass_days": entitlements.PASS_DAYS, "min_available": entitlements.MIN_AVAILABLE,
        "invites_per_opportunity": INVITES_PER_OPPORTUNITY, "network_hourly": POSTS_PER_HOUR, "free_posts": FREE_POSTS_PER_MONTH,
        "has_access": entitlements.has_hiring_access(partner),
        "legacy_pro": entitlements.is_legacy_pro(partner),
        "current_pass": entitlements.current_pass(partner),
        "subscription": getattr(partner, "subscription", None),
        "pending_request": ProUpgradeRequest.objects.filter(partner=partner, product=ProUpgradeRequest.PRODUCT_HIRING_PASS,
                                                            status__in=ProUpgradeRequest.OPEN_STATUSES).first(),
        "legacy_order": ProUpgradeRequest.objects.filter(partner=partner, product=ProUpgradeRequest.PRODUCT_PRO,
                                                         status__in=ProUpgradeRequest.OPEN_STATUSES).first(),
        "stats": pool_stats(),
        "seat": seat,
        "seats_left": entitlements.founding_seats_left(),
        "seat_limit": FoundingSeat.LIMIT,
        "founding_block": None if seat else entitlements.founding_block_reason(partner),
        "claim_form": FoundingClaimForm(opportunities=opportunities),
        "has_opportunities": opportunities.exists(),
        "sales_email": settings.SALES_EMAIL,
    })
    return render(request, "startups/upgrade.html", context)


@login_required
@require_POST
def founding_claim(request):
    """Founding 20: the company asks for its free Hiring Pass. Never automatic; eligibility is checked on the server."""
    from . import entitlements
    from .forms import FoundingClaimForm

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    form = FoundingClaimForm(request.POST, opportunities=entitlements.eligible_opportunities(partner))
    if not form.is_valid():
        messages.error(request, _("Choose one of your open opportunities."))
        return redirect("startup_upgrade")
    try:
        seat = entitlements.claim_founding(partner, form.cleaned_data["internship"])
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Welcome to Founding 20 (seat %(n)s)! Your free Hiring Pass is active until %(date)s.") % {
            "n": seat.number, "date": timezone.localtime(seat.hiring_pass.expires_at).strftime("%Y/%m/%d")})
    return redirect("startup_upgrade")


@login_required
def founding_feedback(request):
    """Optional structured feedback from Founding 20 companies (no answer is required, none is pre-filled)."""
    from django.http import Http404

    from .forms import FoundingFeedbackForm
    from .models import FoundingSeat

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    seat = FoundingSeat.objects.filter(partner=partner).first()
    if seat is None:
        raise Http404
    form = FoundingFeedbackForm(request.POST if request.method == "POST" else None, instance=seat)  # an empty answer is valid
    if request.method == "POST" and form.is_valid():
        seat = form.save(commit=False)
        seat.feedback_at = timezone.now()
        seat.save()
        messages.success(request, _("Thank you! Your feedback goes straight to the founding team."))
        return redirect("startup_upgrade")
    context = get_user_context(request)
    context.update({"form": form, "seat": seat})
    return render(request, "startups/founding_feedback.html", context)


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


@login_required
def talent_pool_preview(request):
    """Free for every company account: real verified counts and anonymised summaries, so nobody pays blind (H-3).

    Names, universities, bios, photos, contacts and candidate ids are never sent; the full search stays Pro-only.
    """
    from django.http import Http404

    from internest_skills.verification import anonymous_previews, parse_id, pool_stats

    from .tiers import is_pro

    partner = startup_partner(request.user)
    if partner is None:
        raise Http404
    skill_id = parse_id(request.GET.get("skill"))
    if is_pro(partner):
        return redirect(reverse("talent_pool") + (f"?skill={skill_id}" if skill_id else ""))
    stats = pool_stats()
    if not any(r["id"] == skill_id and r["filterable"] for r in stats["per_skill"]):
        skill_id = None
    # Counts are for every company account; candidate summaries only for companies an admin has verified.
    can_see_cards = partner.is_fully_verified
    context = get_user_context(request)
    context.update({
        "stats": stats,
        "previews": anonymous_previews(skill_id) if can_see_cards else [],
        "can_see_cards": can_see_cards,
        "skill_id": skill_id,
    })
    return render(request, "startups/talent_pool_preview.html", context)


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
    from .entitlements import covered_internship_ids

    covered = covered_internship_ids(partner)  # None = legacy Pro (all); a Hiring Pass covers only its own opportunity
    open_posts = Internship.objects.open().filter(partner=partner)
    if covered is not None:
        open_posts = open_posts.filter(pk__in=covered)
    opportunities = list(open_posts.annotate(invites_used=Count("talent_invitations")).order_by("-pk"))
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
    from .entitlements import covers

    internship = Internship.objects.filter(
        pk=request.POST.get("internship") or 0, partner=partner, id__in=Internship.objects.open(),
    ).first()
    if internship is not None and not covers(partner, internship):
        internship = None  # a Hiring Pass invites only to the opportunity it was bought for
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
        "experiences": candidate.gig_experiences.verified().select_related("company"),
        "proof": proof_of_work(candidate),
    })


# ---------------------------------------------------------------- checkout (Fawaterak)
CHECKOUT_OUTCOMES = ("success", "fail", "pending")


@login_required
def checkout(request):
    """Hiring Pass order: the opportunity it is for, buyer details and terms consent, then payment.

    The amount always comes from `pricing.pass_quote` on the server; nothing the browser sends changes it. Manual transfer
    (default): InstaPay in Egypt. The company uploads the receipt and the order waits as "Pending verification" until an
    admin checks it and activates the pass. Fawaterak is used only if configured; returning from it never activates.
    """
    from django.conf import settings

    from . import entitlements, fawaterak
    from .forms import CheckoutForm
    from .models import ProUpgradeRequest
    from .pricing import currency_for_partner, gateway_for, pass_quote, pricing_country

    partner = _startup_or_redirect(request)
    if partner is None:
        return redirect("home_redirect")
    country = pricing_country(request, partner)
    currency = currency_for_partner(request, partner)
    promo = _session_promo(request)
    q = pass_quote(currency, promo)
    online = q is not None and currency == "EGP" and fawaterak.enabled()
    manual_method = ProUpgradeRequest.METHOD_INSTAPAY if currency == "EGP" else ProUpgradeRequest.METHOD_REDOTPAY
    open_orders = ProUpgradeRequest.objects.filter(partner=partner, status__in=ProUpgradeRequest.OPEN_STATUSES)
    pending = open_orders.filter(product=ProUpgradeRequest.PRODUCT_HIRING_PASS).first()
    legacy_order = open_orders.filter(product=ProUpgradeRequest.PRODUCT_PRO).first()  # placed before the pricing change
    if request.method == "POST" and request.POST.get("action") in ("cancel_pending", "cancel_legacy"):
        target = legacy_order if request.POST["action"] == "cancel_legacy" else pending
        if target and target.status == ProUpgradeRequest.STATUS_PENDING:  # unpaid orders only — never a sent receipt
            target.status = ProUpgradeRequest.STATUS_CANCELLED
            target.save(update_fields=["status"])
            messages.info(request, _("Your previous unpaid order was cancelled. You can place a new one now."))
        return redirect("checkout")

    opportunities = entitlements.eligible_opportunities(partner)
    user = request.user
    form = CheckoutForm(request.POST or None, request.FILES or None, egypt=currency == "EGP", manual=not online,
                        opportunities=opportunities, initial={
                            "customer_name": user.get_full_name() or partner.company_name,
                            "customer_email": partner.official_email or user.email,
                            "customer_phone": partner.official_phone or "",
                            "internship": request.GET.get("opportunity") or None,
                        })

    if request.method == "POST" and q is not None and form.is_valid():
        d = form.cleaned_data
        internship = d["internship"]
        if pending:
            messages.info(request, _("You already have an order awaiting payment. Our team will contact you shortly."))
            return redirect("checkout")
        if entitlements.covers(partner, internship):
            messages.info(request, _("This opportunity already has paid access. Nothing was charged."))
            return redirect("startup_upgrade")
        order = ProUpgradeRequest(
            partner=partner, product=ProUpgradeRequest.PRODUCT_HIRING_PASS, internship=internship, months=1,
            list_price=q["list_price"], promo_code=promo, discount_percent=q["discount_percent"], final_price=q["final_price"],
            currency=currency, country=country, gateway=gateway_for(currency), customer_name=d["customer_name"],
            customer_email=d["customer_email"], customer_phone=d["customer_phone"], terms_accepted_at=timezone.now(),
        )
        if not online:
            order.gateway, order.status = manual_method, ProUpgradeRequest.STATUS_VERIFYING
            order.payment_proof, order.payment_reference = d["payment_proof"], d.get("payment_reference", "")
            order.proof_submitted_at = timezone.now()
        from django.db import IntegrityError

        try:
            with transaction.atomic():
                order.save()
        except IntegrityError:  # a second checkout in the same instant: the database keeps only one open order
            messages.info(request, _("You already have an order awaiting payment. Our team will contact you shortly."))
            return redirect("checkout")
        request.session.pop(PROMO_SESSION_KEY, None)
        if online:
            back = {o: request.build_absolute_uri(reverse("checkout_return", args=[order.pk, o])) for o in CHECKOUT_OUTCOMES}
            item = _("Internest Hiring Pass — %(days)s days") % {"days": entitlements.PASS_DAYS}
            try:
                order.payment_url, order.gateway_reference = fawaterak.create_invoice_link(order, item, back)
                order.save(update_fields=["payment_url", "gateway_reference"])
                return redirect(order.payment_url)
            except fawaterak.FawaterakError:
                messages.warning(request, _("Online payment is unavailable right now. Your order is saved and our team will contact you within 24 hours to complete payment."))
                return redirect("startup_upgrade")
        messages.success(request, _("Receipt received! We're verifying your transfer and will activate your Hiring Pass within 24 hours."))
        return redirect("startup_upgrade")

    availability = [{"opportunity": o, "matching": entitlements.matching_candidates(o)} for o in opportunities[:10]]
    context = get_user_context(request)
    context.update({"partner": partner, "quote": q, "currency": currency, "promo": promo, "form": form,
                    "online": online, "pending_request": pending, "legacy_order": legacy_order, "manual_method": manual_method,
                    "legacy_pro": entitlements.is_legacy_pro(partner), "availability": availability,
                    "min_available": entitlements.MIN_AVAILABLE, "pass_days": entitlements.PASS_DAYS,
                    "has_opportunities": bool(availability),
                    "instapay_link": settings.INSTAPAY_LINK, "instapay_ipa": settings.INSTAPAY_IPA,
                    "redotpay_id": settings.REDOTPAY_ID, "sales_email": settings.SALES_EMAIL})
    return render(request, "startups/checkout.html", context)


@login_required
def checkout_return(request, order_id, outcome):
    """Where Fawaterak sends the buyer back. Shows the outcome only — Pro is activated after we confirm the payment."""
    from .models import ProUpgradeRequest

    partner = _startup_or_redirect(request)
    if partner is None or outcome not in CHECKOUT_OUTCOMES:
        return redirect("home_redirect")
    order = ProUpgradeRequest.objects.filter(pk=order_id, partner=partner).first()
    if order is None:
        return redirect("startup_upgrade")
    context = get_user_context(request)
    context.update({"order": order, "outcome": outcome})
    return render(request, "startups/checkout_result.html", context)
