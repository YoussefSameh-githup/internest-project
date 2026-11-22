"""JSON answers for the /api/ai/ endpoints when a site-wide middleware answers instead of the view.

The rate limiter (internest_core.ratelimit) renders the 429 HTML page and the onboarding gates redirect; API clients
get the same JSON error shape as the views ({"error", "code"}) with the original status / Retry-After instead.
"""
from django.http import JsonResponse
from django.utils.translation import gettext as _

API_PREFIX = "/api/ai/"


class APIJSONErrorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path_info.startswith(API_PREFIX) or response.get("Content-Type", "").startswith("application/json"):
            return response
        if response.status_code == 429:
            json_response = JsonResponse({"error": _("Too many requests. Please wait a few minutes and try again."),
                                          "code": "rate_limited"}, status=429)
            json_response["Retry-After"] = response.get("Retry-After", "60")
        elif response.status_code in (301, 302) and response.get("Location"):
            json_response = JsonResponse({"error": _("Please finish the step shown on the site first."),
                                          "code": "action_required", "redirect": response["Location"]}, status=403)
        else:
            return response
        json_response["Cache-Control"] = "no-store"
        return json_response
