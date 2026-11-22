"""Async jobs of the career features (registered at startup by internest_core.async_tasks.autodiscover).

Each returns (http_status, json_body), the shape internest_core.async_tasks expects; the poller follows `redirect`.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.translation import gettext as _

from internest_core.async_tasks import register


def _user(user_id):
    return get_user_model().objects.filter(pk=user_id).first()


def _kit_body(kit, status: str) -> dict:
    from .interview_service import describe_ai_status

    url = reverse("interview_kit", args=[kit.pk])
    return {"kit_id": kit.pk, "redirect": url, "kit_url": url, "api_url": reverse("api_interview_kit", args=[kit.pk]),
            "ai_status": status, "message": describe_ai_status(status)}


@register("interview_kit")
def interview_kit_job(user_id, target, seniority="intern", role="", ai=False):
    from .interview_service import KitDenied, create_kit

    user = _user(user_id)
    try:
        kit = create_kit(user, target, seniority=seniority, role=role, use_ai=ai)
    except KitDenied:
        return 404, {"error": _("This interview kit is not available."), "code": "not_found"}
    return 200, _kit_body(kit, kit.ai_status)


@register("interview_kit_enrich")
def interview_kit_enrich_job(user_id, kit_id):
    from .interview_service import KitDenied, enrich, own_kit

    try:
        kit = own_kit(_user(user_id), kit_id)
        status = enrich(kit) if kit.can_enrich else kit.ai_status
    except KitDenied:
        return 404, {"error": _("This interview kit is not available."), "code": "not_found"}
    return 200, _kit_body(kit, status)


@register("career_advice")
def career_advice_job(user_id):
    from internest_ai.errors import AIUnavailable, QuotaExceeded
    from internest_core import identity

    from .career_service import advice

    user = _user(user_id)
    student = identity.student_profile(user)
    if student is None:
        return 404, {"error": _("Career recommendations are for student accounts."), "code": "students_only"}
    try:
        return 200, advice(student, user)
    except QuotaExceeded as exc:
        return 429, {"error": exc.user_message, "code": "ai_quota", "retry_after": exc.retry_after}
    except AIUnavailable:
        return 503, {"error": _("Our AI is busy right now. Your recommendations above are complete without it; try again in a few minutes."),
                     "code": "ai_unavailable"}
