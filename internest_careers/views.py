"""Pages: interview kits (partners), interview practice and career paths (students). Logic lives in the services."""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.utils.translation import gettext as _

from internest_core import async_tasks, identity
from internest_core.models import Application, Internship
from internest_skills.views import student_gate

from . import ai_tasks, career_service
from . import interview_service as kits
from .models import InterviewKit

logger = logging.getLogger(__name__)
PRACTICE_KITS_SHOWN = 6


# ---------------------------------------------------------------- shared by the pages and the JSON API
def kit_request(user, data) -> dict:
    """Normalised job arguments from a form / JSON body. Raises KitDenied when the target is not the user's."""
    spec = {}
    for key, field in (("application", "application_id"), ("internship", "internship_id")):
        raw = str(data.get(field) or data.get(key) or "").strip()
        if raw.isdigit():
            spec[key] = int(raw)
            break
    else:
        role = str(data.get("role") or "").strip()[:120]
        if role:
            spec["role"] = role
    target = str(data.get("target") or "")  # practice form: "internship:<id>" or "role:<key>"
    kind, _sep, value = target.partition(":")
    if not spec and kind == "internship" and value.isdigit():
        spec = {"internship": int(value)}
    elif not spec and kind == "role" and value:
        spec = {"role": value[:120]}
    kits.resolve_target(user, spec)  # access check before anything is queued
    wants_ai = str(data.get("ai") or "").lower() in ("1", "true", "on", "yes")
    return {"target": spec, "seniority": kits.clean_seniority(data.get("seniority")),
            "role": " ".join(str(data.get("role_title") or "").split())[:120],
            "ai": wants_ai and ai_tasks.available(ai_tasks.INTERVIEW_TASK)}


def enqueue_kit(request, job: dict):
    """202 (+ polling) or 429 / 503: the same answer for the page's async form and the JSON API."""
    key = request.headers.get("Idempotency-Key", "").strip()[:64] or None
    return async_tasks.enqueue_response("interview_kit", request.user, idempotency_key=key, **job)


def _classic_create(request, job: dict, fallback: str):
    """No-JS form post: run the job (inline on the sync backend) and go to the kit, or explain the wait."""
    try:
        task = async_tasks.enqueue("interview_kit", request.user, idempotency_key=async_tasks.idempotency_key_for(
            "interview_kit", job), **job)
    except async_tasks.TaskThrottled as exc:
        messages.warning(request, exc.message)
        return redirect(fallback)
    result = task.result or {}
    if task.is_finished and result.get("redirect"):
        if result.get("message"):
            messages.info(request, result["message"])
        return redirect(result["redirect"])
    messages.info(request, _("Your interview kit is being prepared. It will appear in this list in a moment."))
    return redirect(fallback)


def _create(request, fallback: str):
    try:
        job = kit_request(request.user, request.POST)
    except kits.KitDenied:
        raise Http404
    if async_tasks.wants_async(request):
        return enqueue_kit(request, job)
    return _classic_create(request, job, fallback)


def _partner_or_404(request):
    partner = identity.partner_profile(request.user)
    if partner is None:
        raise Http404
    return partner


# ---------------------------------------------------------------- partners
@login_required
def interview_kits(request):
    """A company's kits, plus "new kit for one of my opportunities". Students practise on their own page."""
    if identity.partner_profile(request.user) is None:
        return redirect("interview_practice")
    partner = _partner_or_404(request)
    context = {
        "kits": kits.kits_for(request.user),
        "internships": Internship.objects.filter(partner=partner).order_by("-created_at").only("id", "title")[:50],
        "seniorities": InterviewKit.SENIORITY_CHOICES, "ai_available": ai_tasks.available(ai_tasks.INTERVIEW_TASK),
    }
    return render(request, "careers/interview_kits.html", context)


@login_required
def interview_kit_new(request):
    """Kit for one applicant (?application=) or one opportunity (?internship=), from the applicant / inbox pages."""
    partner = _partner_or_404(request)
    if request.method == "POST":
        return _create(request, reverse("interview_kits"))
    app = internship = None
    if str(request.GET.get("application", "")).isdigit():
        app = (Application.objects.select_related("internship", "applicant")
               .filter(pk=request.GET["application"], internship__partner=partner).first())
        if app is None:
            raise Http404
        internship = app.internship
    elif str(request.GET.get("internship", "")).isdigit():
        internship = Internship.objects.filter(pk=request.GET["internship"], partner=partner).first()
        if internship is None:
            raise Http404
    else:
        return redirect("interview_kits")
    from internest_startups.entitlements import covers

    context = {"app": app, "internship": internship, "covered": covers(partner, internship),
               "seniorities": InterviewKit.SENIORITY_CHOICES, "ai_available": ai_tasks.available(ai_tasks.INTERVIEW_TASK)}
    return render(request, "careers/interview_kit_new.html", context)


# ---------------------------------------------------------------- students
@student_gate()
def interview_practice(request):
    """Practice for an opportunity (applied or open) or a career path; recent practice kits below the form."""
    if request.method == "POST":
        return _create(request, reverse("interview_practice"))
    roles = career_service.recommendations(request.student)["roles"]
    applied = list(Application.objects.filter(applicant=request.user).select_related("internship")
                   .order_by("-application_date").only("internship__id", "internship__title")[:20])
    options = [("internship:%s" % a.internship.id, a.internship.title) for a in applied]
    wanted = ""
    if str(request.GET.get("internship", "")).isdigit():
        wanted = "internship:%s" % request.GET["internship"]
        if wanted not in dict(options):
            extra = Internship.objects.open().filter(pk=request.GET["internship"]).only("id", "title").first()
            if extra is not None:
                options.insert(0, (wanted, extra.title))
            else:
                wanted = ""
    elif request.GET.get("role"):
        wanted = "role:%s" % request.GET["role"]
    context = {
        "hub_tab": "interview", "internship_options": options,
        "role_options": [("role:%s" % r["key"], r["title"]) for r in roles], "selected": wanted,
        "kits": kits.kits_for(request.user, PRACTICE_KITS_SHOWN),
        "seniorities": InterviewKit.SENIORITY_CHOICES, "ai_available": ai_tasks.available(ai_tasks.INTERVIEW_TASK),
    }
    return render(request, "careers/interview_practice.html", context)


@student_gate()
def career_paths(request):
    context = {"hub_tab": "careers", "recs": career_service.recommendations(request.student),
               "ai_available": ai_tasks.available(ai_tasks.CAREER_TASK)}
    return render(request, "careers/career_paths.html", context)


# ---------------------------------------------------------------- one kit
def _kit_or_404(request, kit_id):
    try:
        return kits.own_kit(request.user, kit_id)
    except kits.KitDenied:
        raise Http404


def _kit_context(request, kit):
    applicant = kit.application.applicant if kit.application_id and kit.application else None
    return {"kit": kit, "questions": kit.questions, "practice": kit.is_practice,
            "applicant_name": (applicant.get_full_name() or applicant.username) if applicant else "",
            "ai_message": kits.describe_ai_status(kit.ai_status),
            "can_enrich": kit.can_enrich and ai_tasks.available(ai_tasks.INTERVIEW_TASK),
            "back_url": reverse("interview_practice" if kit.is_practice else "interview_kits")}


@login_required
def interview_kit(request, kit_id):
    kit = _kit_or_404(request, kit_id)
    context = _kit_context(request, kit)
    if kit.is_practice:
        context["hub_tab"] = "interview"
    return render(request, "careers/interview_kit.html", context)


@login_required
def interview_kit_print(request, kit_id):
    return render(request, "careers/interview_kit_print.html", _kit_context(request, _kit_or_404(request, kit_id)))


@login_required
@require_POST
def interview_kit_enrich(request, kit_id):
    kit = _kit_or_404(request, kit_id)
    if not (kit.can_enrich and ai_tasks.available(ai_tasks.INTERVIEW_TASK)):
        messages.info(request, kits.describe_ai_status(kit.ai_status) or _("AI questions are not available right now."))
        return redirect("interview_kit", kit.pk)
    if async_tasks.wants_async(request):
        return async_tasks.enqueue_response("interview_kit_enrich", request.user, kit_id=kit.pk)
    try:
        task = async_tasks.enqueue("interview_kit_enrich", request.user, kit_id=kit.pk,
                                   idempotency_key=f"kit:{kit.pk}")
        message = (task.result or {}).get("message") if task.is_finished else _("AI questions are being added. Refresh in a moment.")
    except async_tasks.TaskThrottled as exc:
        message = exc.message
    if message:
        messages.info(request, message)
    return redirect("interview_kit", kit.pk)
