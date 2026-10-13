"""Public policy pages (required for payment-gateway approval): privacy, terms, refund & cancellation."""
import datetime

from django.shortcuts import render

from .views import get_user_context

POLICIES_UPDATED = datetime.date(2026, 10, 13)  # change when a policy's content changes


def _policy(template, page):
    def view(request):
        context = get_user_context(request)
        context.update({"page": page, "updated": POLICIES_UPDATED})
        return render(request, f"legal/{template}.html", context)
    view.__name__ = f"{page}_policy"
    return view


privacy = _policy("privacy", "privacy")
terms = _policy("terms", "terms")
refund_policy = _policy("refund", "refund")
