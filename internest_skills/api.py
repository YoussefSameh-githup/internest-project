"""JSON API for the AI features (features 1-4), under /api/ai/ (routes in internest_skills/urls.py).

    POST /api/ai/cv-analysis/                    multipart cv_file | linkedin_pdf | pasted_text  → 202 {task_id, status_url, ...}
    GET  /api/ai/cv-analysis/<id>/               the analysis (owner only, else 404)            → 200
    POST /api/ai/skills/extract/                 text                                            → 202 (poll the task)
    POST /api/ai/assessments/                    skill_id (reuses the challenge start)           → 202 (result.redirect)
    POST /api/ai/answers/<response_id>/evaluate/ staff reviewers: advisory AI grading            → 202

Same session auth as the site (CSRF stays on: send X-CSRFToken). Anonymous → 401, wrong role → 403, someone else's
object → 404 (no IDOR oracle). Errors are always {"error": <code>, "message": <text>}; 429 / 503 carry Retry-After.
Rate limits reuse internest_core.ratelimit.hit with the windows in API_RULES (JSON 429 instead of the HTML page).
"""
import json
from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from internest_core import async_tasks, ratelimit

from . import cv_analysis
from .extraction import ExtractionError
from .forms import MIN_PASTED_CHARS, SkillSourceForm
from .models import ChallengeAttempt, ChallengeItem, ChallengeResponse, CVAnalysis, Skill
from .views import NOT_TESTABLE, start_cv_analysis, student_gate, student_skill_for_test

# url name → (max requests, window seconds), per signed-in user.
API_RULES = {
    "skills_api_cv_analysis": (10, 60 * 60),
    "skills_api_skills_extract": (20, 60 * 60),
    "skills_api_assessments": (20, 60 * 60),
    "skills_api_answer_evaluate": (120, 60 * 60),
}
MAX_EXTRACT_CHARS = 20_000


def error(code: str, message: str, status: int, retry_after: int | None = None, **extra) -> JsonResponse:
    response = JsonResponse({"error": code, "message": str(message), **extra}, status=status)
    response["Cache-Control"] = "no-store"
    if retry_after:
        response["Retry-After"] = str(int(retry_after))
    return response


def api_view(name: str):
    """401 JSON for anonymous callers, then the per-user rate limit of API_RULES[name]."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return error("login_required", _("Please sign in first."), 401)
            limit, window = API_RULES[name]
            if settings.RATELIMIT_ENABLED and request.method == "POST" and not ratelimit.hit(f"rl:{name}:u{request.user.pk}", limit, window):
                return error("rate_limited", _("Too many requests. Please try again later."), 429, retry_after=window)
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def _throttled(exc: async_tasks.TaskThrottled) -> JsonResponse:
    return error(exc.code, exc.message, exc.status, retry_after=exc.retry_after)


def _body(request) -> dict:
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return request.POST


# ---------------------------------------------------------------- feature 1: CV analysis
@api_view("skills_api_cv_analysis")
@student_gate(api=True)
@require_POST
def cv_analysis_create(request):
    form = SkillSourceForm(request.POST, request.FILES)
    if not form.is_valid():
        return error("invalid", _("Please correct the errors."), 400, fields=form.errors.get_json_data())
    data = form.cleaned_data
    if not (data.get("cv_file") or data.get("linkedin_pdf") or data.get("pasted_text")):
        return error("nothing_to_analyze", _("Upload your CV or LinkedIn PDF, or paste your LinkedIn / CV text."), 400)
    try:
        started = start_cv_analysis(request, request.student, data)
    except ExtractionError as exc:
        return error("unreadable_file", str(exc), 400)
    if started.outcome == "limit":
        return error("daily_limit", _("Daily analysis limit reached. Try again tomorrow."), 429,
                     retry_after=cv_analysis.seconds_until_tomorrow())
    if started.outcome == "throttled":
        return _throttled(started.error)
    body = cv_analysis.payload(started.analysis)
    task = started.task
    body.update({"analysis_id": started.analysis.pk, "reused": started.outcome == "reused",
                 "task_id": str(task.pk) if task else None,
                 "task_status_url": reverse("async_task_status", args=[task.pk]) if task else None})
    response = JsonResponse(body, status=200 if started.outcome == "reused" else 202)
    response["Location"] = body["status_url"]
    response["Cache-Control"] = "no-store"
    return response


@api_view("skills_api_cv_analysis")
@require_GET
def cv_analysis_detail(request, analysis_id):
    analysis = get_object_or_404(CVAnalysis, pk=analysis_id, user=request.user)  # someone else's id → 404
    response = JsonResponse(cv_analysis.payload(analysis))
    response["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- feature 2: skill extraction
@api_view("skills_api_skills_extract")
@student_gate(api=True)
@require_POST
def skills_extract(request):
    text = _body(request).get("text")
    text = text.strip() if isinstance(text, str) else ""
    if not MIN_PASTED_CHARS <= len(text) <= MAX_EXTRACT_CHARS:
        return error("invalid_text", _("Send between %(min)s and %(max)s characters of text.") % {
            "min": MIN_PASTED_CHARS, "max": MAX_EXTRACT_CHARS}, 400)
    try:
        task = async_tasks.enqueue("skill_extract", request.user, idempotency_key=cv_analysis.fingerprint([], text), text=text)
    except async_tasks.TaskThrottled as exc:
        return _throttled(exc)
    return async_tasks.accepted(task)


# ---------------------------------------------------------------- feature 3: assessment generation
@api_view("skills_api_assessments")
@student_gate(api=True)
@require_POST
def assessments_create(request):
    try:
        skill_id = int(_body(request).get("skill_id"))
    except (TypeError, ValueError):
        return error("invalid", _("skill_id is required."), 400)
    skill = Skill.objects.filter(pk=skill_id, is_active=True).first()
    if skill is None:
        return error("not_found", _("Unknown skill."), 404)
    ss = student_skill_for_test(request.student, skill)
    if ss is None:
        return error("not_testable", NOT_TESTABLE, 403)
    response = async_tasks.enqueue_response("skill_challenge_start", request.user, student_skill_id=ss.pk)
    if response.status_code in (429, 503):  # same {error, message} shape as the rest of this API
        body = json.loads(response.content)
        return error(body.get("code", "throttled"), body.get("error", ""), response.status_code, retry_after=body.get("retry_after"))
    return response


# ---------------------------------------------------------------- feature 4: answer evaluation (advisory)
@api_view("skills_api_answer_evaluate")
@require_POST
def answer_evaluate(request, response_id):
    if not request.user.is_staff:
        return error("forbidden", _("Reviewers only."), 403)
    answer = get_object_or_404(ChallengeResponse.objects.select_related("item", "attempt"), pk=response_id,
                               attempt__review_status=ChallengeAttempt.REVIEW_PENDING)
    if answer.item.kind == ChallengeItem.KIND_MCQ or not (answer.answer_text or "").strip():
        return error("not_gradable", _("Only written answers can be graded."), 400)
    try:
        task = async_tasks.enqueue("answer_evaluation", request.user, idempotency_key=f"evaluate:{answer.pk}", response_id=answer.pk)
    except async_tasks.TaskThrottled as exc:
        return _throttled(exc)
    return async_tasks.accepted(task)
