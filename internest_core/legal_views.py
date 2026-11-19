"""Public trust pages (required for payment gateways, Google OAuth and universities): privacy, terms, refund, contact."""
import datetime

from django.shortcuts import render

from .page_cache import cache_public_page
from .views import get_user_context

POLICIES_UPDATED = datetime.date(2026, 11, 19)  # change when a policy's content changes
PRICING_CHANGED = datetime.date(2026, 11, 18)  # Hiring Pass replaced the Pro subscription


def _policy(template, page):
    @cache_public_page(ttl=3600)
    def view(request):
        context = get_user_context(request)
        from internest_startups.entitlements import MIN_AVAILABLE, PASS_DAYS
        from internest_startups.models import FoundingSeat
        from internest_startups.pricing import HIRING_PASS_PRICES, format_price

        context.update({"page": page, "updated": POLICIES_UPDATED, "change_date": PRICING_CHANGED,
                        "pass_amount": f"{int(HIRING_PASS_PRICES['EGP']):,}", "pass_days": PASS_DAYS,
                        "pass_usd": format_price(HIRING_PASS_PRICES["USD"], "USD"), "pass_eur": format_price(HIRING_PASS_PRICES["EUR"], "EUR"),
                        "min_available": MIN_AVAILABLE, "founding_limit": FoundingSeat.LIMIT})
        return render(request, f"legal/{template}.html", context)
    view.__name__ = f"{page}_policy"
    return view


privacy = _policy("privacy", "privacy")
terms = _policy("terms", "terms")
refund_policy = _policy("refund", "refund")
contact = _policy("contact", "contact")
