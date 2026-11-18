"""Plan rules. Free = 1 opportunity post per calendar month. Paid access (Hiring Pass or a legacy Pro subscription) is
decided in `entitlements.py`; `is_pro` is kept as the short name templates and views already use for "has paid access"."""
from datetime import timedelta

from django.utils import timezone

from internest_core.models import PartnerInternshipSubmission
from internest_skills.models import EmployerSubscription

from .entitlements import forget, has_hiring_access, is_legacy_pro

FREE_POSTS_PER_MONTH = 1
PRO_PERIOD_DAYS = 30  # legacy Pro orders only
PRO_YEAR_DAYS = 365


def is_pro(partner) -> bool:
    """Paid hiring access: an active Hiring Pass or a legacy Pro subscription."""
    return has_hiring_access(partner)


def posts_this_month(partner) -> int:
    start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (
        PartnerInternshipSubmission.objects.filter(partner=partner, submission_date__gte=start)
        .exclude(status="Rejected")
        .count()
    )


def monthly_quota_reached(partner, posts: int | None = None) -> bool:
    """Startups get one opportunity post per calendar month (a Hiring Pass is bought for a posted opportunity and does not
    add posts); legacy Pro subscribers and universities are unlimited. Pass `posts` when already counted for this request."""
    if partner is None or partner.is_academic or is_legacy_pro(partner):
        return False
    return (posts_this_month(partner) if posts is None else posts) >= FREE_POSTS_PER_MONTH


def activate_pro(partner, months=1):
    """Legacy: extend (or start) a Pro subscription. Used only to honour Pro orders placed before 2026-11-18 (and tests)."""
    sub, _created = EmployerSubscription.objects.get_or_create(partner=partner)
    today = timezone.now().date()
    start = sub.valid_until if sub.plan == EmployerSubscription.PLAN_PRO and sub.valid_until and sub.valid_until > today else today
    sub.plan = EmployerSubscription.PLAN_PRO
    sub.valid_until = start + timedelta(days=PRO_YEAR_DAYS if months == 12 else PRO_PERIOD_DAYS * months)
    sub.save()
    forget(partner)
    return sub
