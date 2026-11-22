"""JSON API under /api/ai/ (session auth + CSRF, like the rest of the site). Errors: {"error": message, "code": code}.

    POST /api/ai/interview-kits/            {application_id | internship_id | role, seniority?, role_title?, ai?}
                                            -> 202 {task_id, status_url, ...}; the finished task's result has kit_id
    GET  /api/ai/interview-kits/            the caller's kits (summary)
    GET  /api/ai/interview-kits/<id>/       one kit (owner only, 404 otherwise)
    GET  /api/ai/careers/                   career recommendations (students)
    POST /api/ai/careers/advice/            -> 202; AI narrative from anonymous role / skill data
Resume endpoints live in internest_students/urls.py (same helpers).
"""
import json
from functools import wraps

from django.http import JsonResponse
from django.utils.translation import gettext as _

from internest_core import async_tasks, identity
from internest_skills.permissions import missing_profile_fields

from . import career_service
from . import interview_service as kits
from .views import enqueue_kit, kit_request


def json_error(message, code: str, status: int, **extra) -> JsonResponse:
    response = JsonResponse({"error": str(message), "code": code, **extra}, status=status)
    response["Cache-Control"] = "no-store"
    return response


def api_view(*methods, student=False):
    """Login (401 JSON, never a redirect), allowed methods (405 JSON), optional student-only gate (403 JSON)."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return json_error(_("Please sign in first."), "auth_required", 401)
            if request.method not in methods:
                response = json_error(_("Method not allowed."), "method_not_allowed", 405)
                response["Allow"] = ", ".join(methods)
                return response
            if student:
                profile = identity.student_profile(request.user)
                if profile is None or identity.partner_profile(request.user) is not None:
                    return json_error(_("This is available to student accounts only."), "students_only", 403)
                if missing_profile_fields(profile):
                    return json_error(_("Please complete your basic profile first."), "profile_incomplete", 403)
                request.student = profile
            response = view(request, *args, **kwargs)
            response.setdefault("Cache-Control", "no-store")
            return response
        return wrapper
    return decorator


def json_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@api_view("GET", "POST")
def interview_kits(request):
    if request.method == "GET":
        return JsonResponse({"kits": [{"id": k.pk, "role": k.role, "seniority": k.seniority, "source": k.source,
                                       "questions": len(k.questions), "created_at": k.created_at.isoformat()}
                                      for k in kits.kits_for(request.user)]})
    try:
        job = kit_request(request.user, json_body(request))
    except kits.KitDenied:
        return json_error(_("This applicant, opportunity or role is not available to you."), "not_found", 404)
    return enqueue_kit(request, job)


@api_view("GET")
def interview_kit(request, kit_id):
    try:
        kit = kits.own_kit(request.user, kit_id)
    except kits.KitDenied:
        return json_error(_("This interview kit is not available."), "not_found", 404)
    return JsonResponse(kits.kit_json(kit))


@api_view("GET", student=True)
def careers(request):
    return JsonResponse(career_service.recommendations(request.student))


@api_view("POST", student=True)
def career_advice(request):
    from . import ai_tasks

    if not ai_tasks.available(ai_tasks.CAREER_TASK):
        return json_error(_("AI career advice isn't available right now."), "ai_unavailable", 503)
    return async_tasks.enqueue_response("career_advice", request.user)
