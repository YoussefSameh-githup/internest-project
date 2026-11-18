"""Who may use paid hiring features, and how passes are created. The only place that decides it (Phase 5).

    Free (EGP 0)            company profile, free posting quota, verified pool numbers, anonymous previews
    Hiring Pass (EGP 500)   one opportunity, 30 days, one-time payment, never renews
    Legacy Pro              subscriptions bought before 2026-11-18 keep working until their `valid_until`; not sold any more

Partner-wide paid features (Talent Pool search, candidate profiles, founders network limits) need `has_hiring_access`.
Opportunity features (invitations, applicant scores) need `covers(partner, internship)`: legacy Pro covers all, a pass
covers only its own opportunity. Results are memoised on the partner object, which lives one request.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

PASS_DAYS = 30
MIN_AVAILABLE = 3  # availability policy: fewer matching verified candidates than this at the start → refund on request
_MEMO = "_internest_access"


def _legacy_pro(partner) -> bool:
    sub = getattr(partner, "subscription", None)  # RelatedObjectDoesNotExist is an AttributeError
    return bool(sub and sub.is_pro_active)


def _paid_company(partner) -> bool:
    return partner is not None and not partner.is_academic


def active_passes(partner):
    from .models import HiringPass

    now = timezone.now()
    return HiringPass.objects.filter(partner=partner, starts_at__lte=now, expires_at__gt=now).select_related("internship")


def _memo(partner) -> dict:
    memo = getattr(partner, _MEMO, None)
    if memo is None:
        memo = {}
        setattr(partner, _MEMO, memo)
    return memo


def _legacy(partner) -> bool:
    """Legacy Pro flag only (reads the subscription row, never the passes): cheap enough for every card of a list page."""
    memo = _memo(partner)
    if "legacy" not in memo:
        memo["legacy"] = _legacy_pro(partner)
    return memo["legacy"]


def _access(partner) -> dict:
    """{"legacy": bool, "passes": [HiringPass]} computed once per request; passes are loaded only when needed."""
    memo = _memo(partner)
    if "passes" not in memo:
        memo["passes"] = [] if _legacy(partner) else list(active_passes(partner))
    return {"legacy": memo["legacy"], "passes": memo["passes"]}


def forget(partner):
    """Drop the memo after granting or ending access within the same request."""
    if partner is not None and hasattr(partner, _MEMO):
        delattr(partner, _MEMO)


def has_hiring_access(partner) -> bool:
    if not _paid_company(partner):
        return False
    access = _access(partner)
    return access["legacy"] or bool(access["passes"])


def is_legacy_pro(partner) -> bool:
    return _paid_company(partner) and _legacy(partner)


def covers(partner, internship) -> bool:
    """Paid features for this opportunity: legacy Pro (any), or an active pass bought for this opportunity."""
    if not _paid_company(partner) or internship is None:
        return False
    access = _access(partner)
    internship_id = getattr(internship, "pk", internship)
    return access["legacy"] or any(p.internship_id == internship_id for p in access["passes"])


def covered_internship_ids(partner):
    """Opportunity ids with paid features, or None meaning "all of them" (legacy Pro)."""
    if not _paid_company(partner):
        return set()
    access = _access(partner)
    return None if access["legacy"] else {p.internship_id for p in access["passes"] if p.internship_id}


def current_pass(partner):
    """The active pass that ends last (for "valid until" texts), or None."""
    if not _paid_company(partner):
        return None
    passes = _access(partner)["passes"]
    return max(passes, key=lambda p: p.expires_at) if passes else None


# ---------------------------------------------------------------- availability (refund policy evidence)
def matching_candidates(internship) -> int:
    """Opted-in, active candidates with a verified skill among the opportunity's required skills.
    An opportunity without required skills is matched against the whole verified pool."""
    from internest_skills.verification import pool_candidates, pool_skills

    if internship is None:
        return 0
    skill_ids = list(internship.required_skills.values_list("pk", flat=True))
    if not skill_ids:
        return pool_candidates().count()
    return pool_candidates().filter(pk__in=pool_skills().filter(skill_id__in=skill_ids).values("student_id")).count()


def eligible_opportunities(partner):
    """The company's own open opportunities a pass can be bought for."""
    from internest_core.models import Internship

    return Internship.objects.open().filter(partner=partner).order_by("-pk")


def create_pass(partner, internship, source, order=None):
    """A new 30-day pass. If this opportunity already has a pass running, the new one starts when that one ends, so a
    second payment always buys 30 more days instead of overlapping."""
    from django.db.models import Max

    from .models import HiringPass

    now = timezone.now()
    running_until = HiringPass.objects.filter(partner=partner, internship=internship, expires_at__gt=now).aggregate(m=Max("expires_at"))["m"]
    starts = max(now, running_until) if running_until else now
    hiring_pass = HiringPass.objects.create(
        partner=partner, internship=internship, source=source, order=order, starts_at=starts,
        expires_at=starts + timedelta(days=PASS_DAYS), available_at_start=matching_candidates(internship),
    )
    forget(partner)
    return hiring_pass


# ---------------------------------------------------------------- Founding 20
def founding_seats_left() -> int:
    from .models import FoundingSeat

    return max(FoundingSeat.LIMIT - FoundingSeat.objects.count(), 0)


def founding_block_reason(partner):
    """Why this company cannot claim a Founding 20 seat now, or None if it can."""
    from .models import FoundingSeat

    if not _paid_company(partner):
        return _("Founding 20 is for companies only.")
    if is_legacy_pro(partner):
        return _("Founding 20 is for companies without paid access.")
    if FoundingSeat.objects.filter(partner=partner).exists():
        return _("Your company already has a Founding 20 seat.")
    if not partner.is_fully_verified:
        return _("Founding 20 opens once our team verifies your company.")
    if partner.profile_completion_score < 100:
        return _("Complete your company profile to 100% to claim a Founding 20 seat.")
    if founding_seats_left() == 0:
        return _("All 20 Founding seats have been claimed.")
    return None


def claim_founding(partner, internship):
    """Give the company the next free seat and its free pass. Returns the seat; raises ValueError with a user message."""
    from .models import FoundingSeat

    reason = founding_block_reason(partner)
    if reason:
        raise ValueError(reason)
    if internship is None or not eligible_opportunities(partner).filter(pk=internship.pk).exists():
        raise ValueError(_("Choose one of your open opportunities."))
    if covers(partner, internship) or internship.pass_orders.filter(status__in=("pending", "verifying")).exists():
        raise ValueError(_("This opportunity already has paid access or an order in progress. Choose another one."))
    for _attempt in range(3):  # two companies may take the same number at once: the unique constraint decides
        taken = set(FoundingSeat.objects.values_list("number", flat=True))
        free = [n for n in range(1, FoundingSeat.LIMIT + 1) if n not in taken]
        if not free:
            break
        number = free[0]
        try:
            with transaction.atomic():
                hiring_pass = create_pass(partner, internship, source="founding")
                return FoundingSeat.objects.create(number=number, partner=partner, hiring_pass=hiring_pass)
        except IntegrityError:
            if FoundingSeat.objects.filter(partner=partner).exists():
                raise ValueError(_("Your company already has a Founding 20 seat."))
    raise ValueError(_("All 20 Founding seats have been claimed."))


# ---------------------------------------------------------------- activation of paid orders (admin only)
def activate_order(order_id) -> bool:
    """Turn a paid order into access. Idempotent: an order already activated or cancelled returns False and changes nothing.

    The status flip is a conditional UPDATE, so two admins (or a double click) cannot activate the same order twice;
    the one-pass-per-order constraint is the second guard. A Hiring Pass order whose stored amount does not match the
    server price is refused (rolled back), so a tampered or stale amount never grants access.
    """
    from django.db.models import F

    from .models import PromoCode, ProUpgradeRequest
    from .pricing import pass_quote
    from .tiers import activate_pro

    with transaction.atomic():
        flipped = ProUpgradeRequest.objects.filter(pk=order_id, status__in=ProUpgradeRequest.OPEN_STATUSES).update(
            status=ProUpgradeRequest.STATUS_ACTIVE, activated_at=timezone.now())
        if not flipped:
            return False
        order = ProUpgradeRequest.objects.select_related("partner", "internship", "promo_code").get(pk=order_id)
        if order.product == ProUpgradeRequest.PRODUCT_HIRING_PASS:
            max_discount = order.promo_code.discount_percent if order.promo_code_id else 0
            expected = pass_quote(order.currency, discount_percent=order.discount_percent)
            if (expected is None or order.discount_percent > max_discount
                    or (order.list_price, order.final_price) != (expected["list_price"], expected["final_price"])):
                raise ValueError(f"Order {order.pk}: amount {order.final_price} {order.currency} does not match the Hiring Pass price")
            if order.internship is None or order.internship.partner_id != order.partner_id:
                raise ValueError(f"Order {order.pk}: the opportunity is missing or belongs to another company")
            create_pass(order.partner, order.internship, source="purchase", order=order)
        else:  # historical Pro order placed before 2026-11-18: honoured at the price it was placed at
            activate_pro(order.partner, order.months)
            forget(order.partner)
        if order.promo_code_id:
            PromoCode.objects.filter(pk=order.promo_code_id).update(times_used=F("times_used") + 1)
    return True
