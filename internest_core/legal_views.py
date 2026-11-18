"""Public trust pages (required for payment gateways, Google OAuth and universities): privacy, terms, refund, contact."""
import datetime

from django.shortcuts import render

from .page_cache import cache_public_page
from .views import get_user_context

POLICIES_UPDATED = datetime.date(2026, 11, 18)  # change when a policy's content changes


def _policy(template, page):
    @cache_public_page(ttl=3600)
    def view(request):
        context = get_user_context(request)
        context.update({"page": page, "updated": POLICIES_UPDATED})
        return render(request, f"legal/{template}.html", context)
    view.__name__ = f"{page}_policy"
    return view


privacy = _policy("privacy", "privacy")
terms = _policy("terms", "terms")
refund_policy = _policy("refund", "refund")
contact = _policy("contact", "contact")
