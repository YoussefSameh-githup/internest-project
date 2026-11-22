"""Resume JSON API under /api/ai/resume/ (session auth + CSRF, students only; same error shape as internest_careers.api).

    POST /api/ai/resume/polish/        {"cv": {...}} (default: the saved CV) -> 202 {task_id, status_url}
    GET  /api/ai/resume/ats/           ATS score of the saved CV with concrete fixes
    GET  /api/ai/resume/export.docx    Word export of the saved CV
    GET  /api/ai/resume/export.pdf     PDF export (?template=classic|modern)
"""
from django.http import JsonResponse
from django.utils.translation import gettext as _

from internest_careers.api import api_view, json_body, json_error

from . import cv_builder_service as svc
from .views import export_response, polish_request, saved_cv, template_choice


def _saved_or_404(request):
    cv = saved_cv(request.student)
    if cv is None:
        return None, json_error(_("Save your CV in the builder first."), "no_cv", 404)
    return cv, None


@api_view("POST", student=True)
def resume_polish(request):
    body = json_body(request)
    if isinstance(body.get("cv"), dict):
        cv = svc.clean_cv(body["cv"])
    else:
        cv, error = _saved_or_404(request)
        if error:
            return error
    return polish_request(request, cv, force_async=True)


@api_view("GET", student=True)
def resume_ats(request):
    cv, error = _saved_or_404(request)
    return error or JsonResponse(svc.ats_report(cv))


@api_view("GET", student=True)
def resume_docx(request):
    cv, error = _saved_or_404(request)
    return error or export_response(cv, "docx")


@api_view("GET", student=True)
def resume_pdf(request):
    cv, error = _saved_or_404(request)
    return error or export_response(cv, "pdf", template_choice(request))
