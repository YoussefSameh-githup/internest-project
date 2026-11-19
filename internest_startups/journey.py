"""The free employer journey (Phase 4): where a company stands on its way to hiring, computed from real records.

    account → complete profile → verified by our team → post an opportunity (free) → published after review
    → explore verified talent numbers and anonymous summaries → Hiring Pass (unlock candidate details)

Shown as a status strip on the dashboard (no buttons: the dashboard cards below are the single place for each action).
"""
from django.utils.translation import gettext_lazy as _

STEPS = [
    ("account", _("Account created")),
    ("profile", _("Company profile complete")),
    ("verified", _("Verified by our team")),
    ("posted", _("Opportunity posted for free")),
    ("published", _("Opportunity published after review")),
    ("pass", _("Hiring Pass: unlock candidate details")),
]

NEXT_HINTS = {
    "closed_wait": _("Your opportunity is closed and this month's free post is used. You can post a new one for free on %(date)s."),
    "closed_post": _("Your opportunity is closed. Post a new one for free to continue."),
    "profile": _("Next: complete your company profile to 100%."),
    "verified": _("Next: our team verifies your company, usually within 24 hours. Meanwhile you can see live talent numbers."),
    "posted": _("Next: post your first opportunity. It's free."),
    "published": _("Next: our team reviews your opportunity before students see it. Meanwhile, explore the anonymous candidate summaries."),
    "pass": _("Next: when you're ready to contact candidates, buy one Hiring Pass for your opportunity. Check the live numbers and how we verify skills first."),
}


def next_free_post_date():
    """First day of next month: when the free monthly post comes back."""
    from django.utils import timezone

    today = timezone.localdate()
    return today.replace(year=today.year + (today.month == 12), month=today.month % 12 + 1, day=1)


def employer_journey(partner, active_opportunities: int, quota_reached: bool = False):
    """[{key, label, done, current}] plus the next hint, or None for universities and companies that already pay."""
    from .entitlements import has_hiring_access

    if partner is None or partner.is_academic or has_hiring_access(partner):
        return None
    from internest_core.models import Internship, PartnerInternshipSubmission

    pending = active_opportunities == 0 and PartnerInternshipSubmission.objects.filter(partner=partner, status="Pending").exists()
    had_one = active_opportunities > 0 or pending or Internship.objects.filter(partner=partner).exists()
    posted = had_one
    done = {
        "account": True,
        "profile": partner.profile_completion_score >= 100,
        "verified": bool(partner.is_fully_verified),
        "posted": posted,
        "published": active_opportunities > 0,
        "pass": False,
    }
    current = next((key for key, _label in STEPS if not done[key]), "pass")
    steps = [{"key": key, "label": label, "done": done[key], "current": key == current} for key, label in STEPS]
    hint = NEXT_HINTS[current]
    if current == "published" and not pending:  # it was published and is now closed/expired/removed: nothing is in review
        from django.utils.formats import date_format

        hint = (NEXT_HINTS["closed_wait"] % {"date": date_format(next_free_post_date())} if quota_reached
                else NEXT_HINTS["closed_post"])
    return {"steps": steps, "hint": hint, "done_count": sum(done.values()), "total": len(STEPS)}


def no_open_opportunity_message(partner):
    """What the plans page and checkout say when nothing can be bought yet, without sending anyone into a loop."""
    from django.utils.formats import date_format

    from .tiers import monthly_quota_reached

    if awaiting_publication(partner):
        return "review", None
    if monthly_quota_reached(partner):
        return "quota", date_format(next_free_post_date())
    return "post", None


def awaiting_publication(partner) -> bool:
    """An opportunity was submitted and is still waiting for our review (so nothing can be bought for it yet)."""
    from internest_core.models import PartnerInternshipSubmission

    return PartnerInternshipSubmission.objects.filter(partner=partner, status="Pending").exists()
