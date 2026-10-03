"""Free vs Pro tier rules (BMC): Free = 1 opportunity/month, Pro = unlimited + priority + talent search."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from internest_core.models import PartnerInternshipSubmission
from internest_skills.models import EmployerSubscription
from internest_skills.permissions import is_pro_employer

FREE_POSTS_PER_MONTH = 1
PRO_MONTHLY_PRICE = Decimal("100.00")  # USD, per BMC v2.0
PRO_PERIOD_DAYS = 30


def is_pro(partner) -> bool:
    return is_pro_employer(partner)


def posts_this_month(partner) -> int:
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        PartnerInternshipSubmission.objects.filter(partner=partner, submission_date__gte=start)
        .exclude(status="Rejected")
        .count()
    )


def monthly_quota_reached(partner) -> bool:
    """Free startups get one opportunity per calendar month; Pro and universities are unlimited."""
    if partner is None or partner.is_academic or is_pro(partner):
        return False
    return posts_this_month(partner) >= FREE_POSTS_PER_MONTH


def quote(promo=None, months=1) -> dict:
    list_price = PRO_MONTHLY_PRICE * months
    percent = promo.discount_percent if promo else 0
    discount = (list_price * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"list_price": list_price, "discount_percent": percent, "discount": discount, "final_price": list_price - discount}


def activate_pro(partner, months=1):
    """Extend (or start) the partner's Pro subscription by `months` billing periods."""
    sub, _created = EmployerSubscription.objects.get_or_create(partner=partner)
    today = timezone.now().date()
    start = sub.valid_until if sub.plan == EmployerSubscription.PLAN_PRO and sub.valid_until and sub.valid_until > today else today
    sub.plan = EmployerSubscription.PLAN_PRO
    sub.valid_until = start + timedelta(days=PRO_PERIOD_DAYS * months)
    sub.save()
    return sub
