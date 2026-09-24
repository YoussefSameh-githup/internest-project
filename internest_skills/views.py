import json
import logging
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from internest_core.models import StudentProfile
from internest_core.views import _get_partner_profile, get_user_context

from . import challenge as engine
from .extraction import ExtractionError, extract_skills, read_document_text, save_claimed_skills
from .forms import SkillSourceForm
from .models import ChallengeAttempt, ChallengeItem, SkillProfile, StudentSkill
from .permissions import is_verified_university, missing_profile_fields, skill_view_role, student_for
from .recommendations import recommendations_for

logger = logging.getLogger(__name__)

MAX_EXTRACTIONS_PER_DAY = 5


def student_gate(api=False):
    """Students only (never startups/universities), and only once their basic profile is saved."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            student = student_for(request.user, create=True)
            if student is None:
                if api:
                    return JsonResponse({"error": "students_only"}, status=403)
                raise Http404
            if missing_profile_fields(student):
                if api:
                    return JsonResponse({"error": "profile_incomplete", "profile_url": reverse("profile")}, status=403)
                messages.warning(request, "Please complete your basic profile first (university, major and study level) to analyze and verify your skills.")
                return redirect("profile")
            request.student = student
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def _skills_with_challenge_flag(qs):
    return qs.select_related("skill").annotate(
        active_items=Count("skill__items", filter=Q(skill__items__is_active=True))
    )


# ---------------------------------------------------------------- student hub
@student_gate()
def skills_hub(request):
    student = request.student
    skill_profile, _ = SkillProfile.objects.get_or_create(student=student)

    if request.method == "POST":
        form = SkillSourceForm(request.POST, request.FILES)
        if form.is_valid():
            _handle_extraction(request, student, skill_profile, form.cleaned_data)
            return redirect("skills_hub")
    else:
        form = SkillSourceForm(initial={
            "linkedin_url": skill_profile.linkedin_url or student.linkedin_url,
            "partner_university": skill_profile.partner_university_id,
        })

    skills = _skills_with_challenge_flag(student.skills.all())
    context = get_user_context(request)
    context.update({
        "form": form,
        "skill_profile": skill_profile,
        "verified": [s for s in skills if s.status == StudentSkill.STATUS_VERIFIED],
        "lagging": [s for s in skills if s.status == StudentSkill.STATUS_LAG],
        "claimed": [s for s in skills if s.status == StudentSkill.STATUS_CLAIMED],
    })
    return render(request, "skills/hub.html", context)


def _handle_extraction(request, student, skill_profile, data):
    today = timezone.localdate()
    if skill_profile.extraction_day != today:
        skill_profile.extraction_day = today
        skill_profile.extraction_count_today = 0

    skill_profile.partner_university = data.get("partner_university")
    if data.get("linkedin_url"):
        skill_profile.linkedin_url = data["linkedin_url"]

    files = [f for f in (data.get("cv_file"), data.get("linkedin_pdf")) if f]
    if files and skill_profile.extraction_count_today >= MAX_EXTRACTIONS_PER_DAY:
        skill_profile.save()
        messages.error(request, "Daily analysis limit reached. Try again tomorrow.")
        return

    if files:
        try:
            text = "\n\n".join(read_document_text(f) for f in files)
        except ExtractionError as exc:
            skill_profile.save()
            messages.error(request, str(exc))
            return
        extracted = extract_skills(text)
        created, updated = save_claimed_skills(student, extracted)
        skill_profile.extraction_count_today += 1
        skill_profile.last_extracted_at = timezone.now()
        explicit = sum(1 for e in extracted if e.source == StudentSkill.SOURCE_EXPLICIT)
        messages.success(
            request,
            f"Analysis complete: {explicit} explicit and {len(extracted) - explicit} inferred skills found "
            f"({created} new). They are marked as Claimed until you verify them.",
        )
    else:
        messages.info(
            request,
            "LinkedIn URL saved. LinkedIn does not allow automated profile reading — upload the PDF export "
            "(LinkedIn profile → More → Save to PDF) to analyze it.",
        )
    skill_profile.save()


# ---------------------------------------------------------------- challenge flow
@student_gate()
@require_POST
def challenge_start(request, student_skill_id):
    student = request.student
    with transaction.atomic():
        ss = get_object_or_404(StudentSkill.objects.select_for_update().select_related("skill"), pk=student_skill_id, student=student)
        try:
            attempt = engine.start_attempt(ss)
        except engine.ChallengeError as exc:
            messages.warning(request, str(exc))
            return redirect("skills_hub")
    return redirect("skills_challenge", token=attempt.token)


@student_gate()
@require_GET
def challenge_run(request, token):
    student = request.student
    attempt = get_object_or_404(ChallengeAttempt.objects.select_related("student_skill__skill"), token=token, student_skill__student=student)
    if not attempt.is_active:
        return redirect("skills_result", token=attempt.token)
    context = get_user_context(request)
    context.update({"attempt": attempt, "skill": attempt.student_skill.skill})
    return render(request, "skills/challenge.html", context)


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}


def _attempt_or_404(request, token):
    attempt = engine.active_attempt_for(request.student, token)
    if attempt is None:
        raise Http404
    return attempt


def _next_payload(attempt):
    item = engine.serve_next(attempt) if attempt.is_active else None
    if item is not None:
        return {"state": "active", "item": item}
    return {"state": attempt.state, "result_url": reverse("skills_result", args=[attempt.token])}


@student_gate(api=True)
@require_POST
def api_next(request, token):
    with transaction.atomic():
        attempt = _attempt_or_404(request, token)
        return JsonResponse(_next_payload(attempt))


@student_gate(api=True)
@require_POST
def api_answer(request, token):
    body = _json_body(request)
    try:
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "item_id required"}, status=400)
    choice = body.get("choice_index")
    choice = int(choice) if isinstance(choice, int) or (isinstance(choice, str) and choice.isdigit()) else None
    answer = body.get("answer_text") if isinstance(body.get("answer_text"), str) else ""
    stats = body.get("typing_stats") if isinstance(body.get("typing_stats"), dict) else {}

    with transaction.atomic():
        attempt = _attempt_or_404(request, token)
        try:
            engine.submit_answer(attempt, item_id, choice, answer, stats)
        except engine.ChallengeError as exc:
            payload = _next_payload(attempt)
            payload["error"] = str(exc)
            return JsonResponse(payload, status=409)
        return JsonResponse(_next_payload(attempt))


@student_gate(api=True)
@require_POST
def api_event(request, token):
    kind = _json_body(request).get("type")
    with transaction.atomic():
        attempt = _attempt_or_404(request, token)
        if kind in ("tab_hidden", "window_blur"):
            engine.record_focus_loss(attempt, kind)
        elif kind in ("copy", "cut", "paste", "contextmenu", "drop", "devtools"):
            engine.record_blocked_action(attempt, kind)
        else:
            return JsonResponse({"error": "unknown event"}, status=400)
        payload = {
            "state": attempt.state,
            "focus_warnings": attempt.focus_warnings,
            "max_focus_warnings": ChallengeAttempt.MAX_FOCUS_WARNINGS,
        }
        if not attempt.is_active:
            payload["result_url"] = reverse("skills_result", args=[attempt.token])
        return JsonResponse(payload)


@student_gate()
def challenge_result(request, token):
    student = request.student
    attempt = get_object_or_404(ChallengeAttempt.objects.select_related("student_skill__skill"), token=token, student_skill__student=student)
    if attempt.is_active:
        return redirect("skills_challenge", token=attempt.token)
    ss = attempt.student_skill
    context = get_user_context(request)
    context.update({
        "attempt": attempt,
        "student_skill": ss,
        "upskill": recommendations_for(ss),
    })
    return render(request, "skills/result.html", context)


@student_gate(api=True)
@require_GET
def api_recommendations(request, student_skill_id):
    ss = get_object_or_404(StudentSkill.objects.select_related("skill", "student"), pk=student_skill_id, student=request.student)
    return JsonResponse(recommendations_for(ss))


# ---------------------------------------------------------------- partner / university views
@login_required
def student_skill_report(request, student_id):
    student = get_object_or_404(StudentProfile.objects.select_related("user"), pk=student_id)
    role = skill_view_role(request.user, student)
    if role is None:
        partner = _get_partner_profile(request.user)
        if partner is not None and not partner.is_academic:
            context = get_user_context(request)
            context["student"] = student
            return render(request, "skills/pro_required.html", context, status=403)
        return HttpResponseForbidden("You are not authorized to view this student's skill data.")
    context = get_user_context(request)
    context.update({
        "student": student,
        "viewer_role": role,
        "skills": student.skills.select_related("skill").order_by("-status", "-percentile", "skill__name"),
    })
    return render(request, "skills/report.html", context)


@login_required
def university_dashboard(request):
    partner = _get_partner_profile(request.user)
    if not is_verified_university(partner):
        return HttpResponseForbidden("Available to verified partner universities only.")
    students = StudentProfile.objects.filter(skill_profile__partner_university=partner).select_related("user")
    total_students = students.count()
    verified_qs = StudentSkill.objects.filter(student__in=students, status=StudentSkill.STATUS_VERIFIED)
    employable = verified_qs.values("student").distinct().count()
    by_skill = (
        StudentSkill.objects.filter(student__in=students)
        .values("skill__name", "skill__discipline")
        .annotate(
            claimed=Count("id"),
            verified=Count("id", filter=Q(status=StudentSkill.STATUS_VERIFIED)),
            lagging=Count("id", filter=Q(status=StudentSkill.STATUS_LAG)),
            avg_percentile=Avg("percentile", filter=Q(status=StudentSkill.STATUS_VERIFIED)),
        )
        .order_by("-verified", "skill__name")
    )
    per_student = students.annotate(
        verified_count=Count("skills", filter=Q(skills__status=StudentSkill.STATUS_VERIFIED)),
        lag_count=Count("skills", filter=Q(skills__status=StudentSkill.STATUS_LAG)),
    ).order_by("-verified_count", "user__first_name")
    context = get_user_context(request)
    context.update({
        "partner": partner,
        "total_students": total_students,
        "employable": employable,
        "employability_rate": round(employable / total_students * 100) if total_students else 0,
        "by_skill": by_skill,
        "per_student": per_student,
    })
    return render(request, "skills/university.html", context)
