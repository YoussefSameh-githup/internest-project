import json
import logging
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _, gettext_lazy as _lazy
from django.views.decorators.http import require_GET, require_POST

from internest_core.api_responses import json_error
from internest_core import async_tasks
from internest_core.models import Internship, StudentProfile
from internest_core.views import _get_partner_profile, get_user_context

from . import challenge as engine
from . import cv_analysis
from .extraction import ExtractionError
from . import freelance_service
from .forms import CertificateForm, FreelanceImportForm, GigProofForm, SkillSourceForm, UniversityShareForm
from .llm import llm_enabled
from .models import (
    ChallengeAttempt, ChallengeItem, CVAnalysis, Discipline, Skill, SkillProfile, StudentFreelanceGig, StudentSkill,
)
from .permissions import is_verified_university, missing_profile_fields, skill_view_role, student_for
from .quizgen import quiz_item_ids_for, schedule_verification
from .recommendations import recommendations_for

logger = logging.getLogger(__name__)

MAX_EXTRACTIONS_PER_DAY = cv_analysis.MAX_ANALYSES_PER_DAY
MAX_FREELANCE_IMPORTS_PER_DAY = 5
UNIVERSITY_STUDENTS_PER_PAGE = 25
NOT_TESTABLE = _lazy("You can take a test for skills in your profile, or for skills an open opportunity requires. "
                     "Analyze your CV first to add this skill.")
OWNERSHIP_WARNING = _lazy(
    "Note: the username in the platform link doesn't directly match the account owner's name. Please upload a "
    "screenshot that shows you own the account to speed up verification.")


def student_gate(api=False):
    """Students only (never startups/universities), and only once their basic profile is saved."""
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            student = student_for(request.user, create=True)
            if student is None:
                if api:
                    return json_error(_("This is available to student accounts only."), "students_only", 403)
                raise Http404
            if missing_profile_fields(student):
                if api:
                    return json_error(_("Please complete your basic profile first."), "profile_incomplete", 403,
                                      profile_url=reverse("profile"))
                messages.warning(request, _("Please complete your basic profile first (university, major and study level) to analyze and verify your skills."))
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
    skill_profile, _created = SkillProfile.objects.select_related("last_analysis").get_or_create(student=student)

    if request.method == "POST":
        form = SkillSourceForm(request.POST, request.FILES)
        if form.is_valid():
            _handle_extraction(request, student, skill_profile, form.cleaned_data)
            target = request.POST.get("next", "")  # e.g. the Student Hub dialog sends people back to their skills tab
            if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
                return redirect(target)
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
        "analysis": cv_analysis.view_context(skill_profile.last_analysis),
        "verified": [s for s in skills if s.status == StudentSkill.STATUS_VERIFIED],
        "lagging": [s for s in skills if s.status == StudentSkill.STATUS_LAG],
        "in_review": [s for s in skills if s.status == StudentSkill.STATUS_REVIEW],
        "claimed": [s for s in skills if s.status == StudentSkill.STATUS_CLAIMED],
        "can_generate_quiz": llm_enabled(),
        "top_domains": [Discipline(d).label for d in skill_profile.top_domains if d in Discipline.values],
    })
    return render(request, "skills/hub.html", context)


def start_cv_analysis(request, student, data):
    """Shared by the hub form and POST /api/ai/cv-analysis/: read the text locally, then queue the analysis.
    Returns cv_analysis.Started; raises ExtractionError for an unreadable file (zip bomb, scanned PDF, ...)."""
    pasted = data.get("pasted_text") or ""
    files = [f for f in (data.get("cv_file"), data.get("linkedin_pdf")) if f]
    sha = cv_analysis.fingerprint(files, pasted)
    text, source = cv_analysis.read_sources(data.get("cv_file"), data.get("linkedin_pdf"), pasted)
    return cv_analysis.start(request.user, student, text, sha=sha, source=source)


def _handle_extraction(request, student, skill_profile, data):
    if data.get("linkedin_url") and data["linkedin_url"] != skill_profile.linkedin_url:
        SkillProfile.objects.filter(pk=skill_profile.pk).update(linkedin_url=data["linkedin_url"])
    if not (data.get("cv_file") or data.get("linkedin_pdf") or data.get("pasted_text")):
        messages.info(
            request,
            _("LinkedIn URL saved. To analyze your skills, upload your CV or LinkedIn PDF, or paste your "
              "About, Experience and Skills text."),
        )
        return None
    try:
        started = start_cv_analysis(request, student, data)
    except ExtractionError as exc:
        messages.error(request, str(exc))
        return None
    if started.outcome == "limit":
        messages.error(request, _("Daily analysis limit reached. Try again tomorrow."))
    elif started.outcome == "throttled":
        messages.error(request, started.error.message)
    elif started.analysis is not None and started.analysis.is_active:
        messages.info(request, _("We're analyzing your CV. Your results appear below in a few seconds."))
    else:
        message = cv_analysis.hub_message(started.analysis)
        if message:
            getattr(messages, message[0])(request, message[1])
    return started.analysis


@student_gate()
@require_POST
def cv_confirm_skills(request, analysis_id):
    """Add the AI-suggested skills the student ticked (only ones this analysis offered) as Claimed, provenance inferred."""
    analysis = get_object_or_404(CVAnalysis, pk=analysis_id, user=request.user, status=CVAnalysis.STATUS_DONE)
    ids = [int(v) for v in request.POST.getlist("skill_ids") if v.isdigit()]
    added = cv_analysis.confirm_skills(analysis, request.student, ids)
    if added:
        messages.success(request, _("%(n)s skills added to your claimed skills. Verify them with a challenge.") % {"n": added})
    else:
        messages.info(request, _("Tick the skills you really have, then add them."))
    return redirect(reverse("skills_hub") + "#claimed")


# ---------------------------------------------------------------- freelance importer
@student_gate()
def freelance_importer(request):
    student = request.student
    gigs = student.freelance_gigs.prefetch_related("skills")
    form = FreelanceImportForm(request.POST or None, request.FILES or None, student=student)
    ownership_warning = False
    if request.method == "POST" and form.is_valid():
        since = timezone.now() - timedelta(days=1)
        if gigs.filter(created_at__gte=since).count() >= MAX_FREELANCE_IMPORTS_PER_DAY:
            messages.error(request, _("You can import up to %(n)s gigs a day. Try again tomorrow.") % {"n": MAX_FREELANCE_IMPORTS_PER_DAY})
            return redirect("freelance_importer")
        d = form.cleaned_data
        _handle, matched = freelance_service.ownership_check(student, d["link"])
        if matched is False and not d.get("proof") and not request.POST.get("confirm_unmatched"):
            # Soft check: ask for a screenshot first, but let the student import anyway.
            ownership_warning = True
        else:
            return _import_freelance_gig(request, student, d, matched)
    context = get_user_context(request)
    context.update({"form": form, "gigs": gigs, "platforms": StudentFreelanceGig.PLATFORM_CHOICES,
                    "ownership_warning": ownership_warning})
    return render(request, "skills/freelance_importer.html", context)


def _import_freelance_gig(request, student, d, matched):
    gig = freelance_service.import_gig(student, d["platform"], d["link"], d["raw_text"], d.get("proof"))
    names = ", ".join(gig.skills.values_list("name", flat=True))
    if gig.status == StudentFreelanceGig.STATUS_VERIFIED:
        messages.success(request, _("Gig imported and verified. Your skills now carry the “Verified skill ✅” badge: %(skills)s") % {"skills": names})
    elif names:
        messages.success(request, _("Gig imported. We found: %(skills)s. Our team reviews the link and proof, usually within 48 hours, then your skills get the “Verified skill ✅” badge.") % {"skills": names})
    else:
        messages.warning(request, _("Gig saved, but we couldn't find skills from our list in the text. Add more detail about what you delivered (tools, tasks, results)."))
    if matched is False:
        messages.warning(request, OWNERSHIP_WARNING)
    return redirect("freelance_importer")


@student_gate()
@require_POST
def freelance_gig_proof(request, gig_id):
    """Attach a screenshot/PDF proving account ownership to a gig still under review."""
    gig = get_object_or_404(StudentFreelanceGig, pk=gig_id, student=request.student,
                            status__in=[StudentFreelanceGig.STATUS_PENDING, StudentFreelanceGig.STATUS_NEEDS_PROOF])
    form = GigProofForm(request.POST, request.FILES)
    if form.is_valid():
        if gig.proof:
            gig.proof.delete(save=False)
        gig.proof = form.cleaned_data["proof"]
        gig.save(update_fields=["proof"])
        messages.success(request, _("Proof uploaded. Our team will review it soon."))
    else:
        messages.error(request, " ".join(form.errors.get("proof", [])))
    return redirect(f"{reverse('profile')}?tab=activity")


@student_gate()
@require_POST
def skills_share_university(request):
    form = UniversityShareForm(request.POST)
    if form.is_valid():
        skill_profile, _created = SkillProfile.objects.get_or_create(student=request.student)
        skill_profile.partner_university = form.cleaned_data["partner_university"]
        skill_profile.save(update_fields=["partner_university"])
        messages.success(request, _("Your sharing preference was saved."))
    return redirect(f"{reverse('profile')}?tab=edit")


# ---------------------------------------------------------------- market fit & certificates
@student_gate()
def student_dashboard(request):
    """Market Fit: readiness index, radar (student vs. market) and the top missing skills with next steps."""
    from .market_fit import market_fit
    from .models import SkillCertificate
    from .recommendations import courses_for_skill

    field = request.GET.get("field", "")
    fit = market_fit(request.student, field if field in Discipline.values else None)
    context = get_user_context(request)
    context.update({
        "fit": fit,
        "radar": fit.radar,
        "field": field if field in Discipline.values else "",
        "fields": Discipline.choices,
        "gaps": [{"skill": s, "demand": n, "courses": courses_for_skill(s)} for s, n in fit.gaps],
        "certificate_form": CertificateForm(),
        "certificates": SkillCertificate.objects.filter(student=request.student).select_related("skill")[:10],
        "page": "market_fit",
    })
    return render(request, "skills/market_fit.html", context)


@student_gate()
@require_POST
def certificate_submit(request):
    from .certificates import submit_certificate

    form = CertificateForm(request.POST, request.FILES)
    if form.is_valid():
        d = form.cleaned_data
        submit_certificate(request.student, d["skill"], d["title"], d["issuer"], d["url"], d.get("file"))
        messages.success(request, _("Certificate received. Your %(skill)s skill is under review — once we confirm it, it gets the “Verified skill ✅” badge.") % {"skill": d["skill"].name})
    else:
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse("student_dashboard")
    return redirect(target)


# ---------------------------------------------------------------- challenge flow
CHALLENGE_STARTED, CHALLENGE_UNAVAILABLE, CHALLENGE_REFUSED = "started", "unavailable", "refused"


def start_challenge(student, student_skill_id):
    """Prepare the quiz (AI first, else the saved bank) and open the attempt.

    Returns (outcome, value): (STARTED, attempt) | (UNAVAILABLE, student_skill) | (REFUSED, message).
    Shared by the synchronous view and the async job, so both behave identically."""
    ss = get_object_or_404(StudentSkill.objects.select_related("skill"), pk=student_skill_id, student=student)
    item_ids = None
    # Live quiz generation (network) happens outside the row lock; skipped when resuming or not eligible.
    can_start = ss.status not in (StudentSkill.STATUS_VERIFIED, StudentSkill.STATUS_REVIEW) and not ss.in_cooldown
    if can_start and not ss.attempts.filter(state=ChallengeAttempt.STATE_ACTIVE).exists():
        try:
            item_ids = quiz_item_ids_for(ss.skill, ss, user=student.user_id)  # AI first (daily quota), else the bank
        except Exception:
            logger.exception("Preparing a quiz failed for skill %s", ss.skill_id)
            item_ids = []
        if not item_ids:
            return CHALLENGE_UNAVAILABLE, ss
    try:
        with transaction.atomic():
            ss = get_object_or_404(StudentSkill.objects.select_for_update().select_related("skill"), pk=student_skill_id, student=student)
            attempt = engine.start_attempt(ss, item_ids=item_ids)
            if item_ids:  # a fresh AI quiz: a second model re-checks its answer keys once this is committed
                schedule_verification(attempt, student.user_id)
    except engine.ChallengeError as exc:  # cooldown, already verified, abandoned session
        return CHALLENGE_REFUSED, str(exc)
    except Http404:
        raise
    except Exception:
        logger.exception("Starting a challenge failed for student skill %s", student_skill_id)
        return CHALLENGE_UNAVAILABLE, ss
    return CHALLENGE_STARTED, attempt


def challenge_outcome(student, student_skill_id) -> tuple[int, dict]:
    """JSON form of start_challenge for the async path: {redirect} on success, {error[, redirect]} otherwise."""
    outcome, value = start_challenge(student, student_skill_id)
    if outcome == CHALLENGE_STARTED:
        return 200, {"redirect": reverse("skills_challenge", kwargs={"token": value.token})}
    if outcome == CHALLENGE_REFUSED:
        return 409, {"error": value, "code": "challenge_unavailable", "redirect": reverse("skills_hub")}
    return 503, {"code": "no_questions", "error": _("Not enough questions are available for this challenge right now. Please try again later.")}


@student_gate()
@require_POST
def challenge_start(request, student_skill_id):
    if async_tasks.wants_async(request):  # quiz generation can take 5-60 s: answer 202 and let the page poll
        get_object_or_404(StudentSkill, pk=student_skill_id, student=request.student)  # ownership checked before queueing
        return async_tasks.enqueue_response("skill_challenge_start", request.user, student_skill_id=student_skill_id)
    outcome, value = start_challenge(request.student, student_skill_id)
    if outcome == CHALLENGE_UNAVAILABLE:
        return _quiz_unavailable(request, value)
    if outcome == CHALLENGE_REFUSED:
        messages.warning(request, value)
        return redirect("skills_hub")
    return redirect("skills_challenge", token=value.token)


def _quiz_unavailable(request, student_skill):
    """Friendly 200 page when neither the AI nor the saved bank can supply a quiz."""
    context = get_user_context(request)
    context.update({
        "skill": student_skill.skill,
        "message": _("Not enough questions are available for this challenge right now. Please try again later."),
    })
    return render(request, "skills/challenge_unavailable.html", context)


def student_skill_for_test(student, skill):
    """The StudentSkill a test of `skill` runs on, or None when the student may not test it (audit M2): only skills
    they already claimed, or skills an open opportunity requires (claimed on the fly), so nobody can make the AI
    write quizzes for every skill in the taxonomy."""
    ss = StudentSkill.objects.filter(student=student, skill=skill).first()
    if ss is not None:
        return ss
    if not Internship.objects.open().filter(required_skills=skill).exists():
        return None
    ss, _created = StudentSkill.objects.get_or_create(
        student=student, skill=skill, defaults={"source": StudentSkill.SOURCE_EXPLICIT, "evidence": ""})
    return ss


@student_gate()
@require_POST
def challenge_start_for_skill(request, skill_id):
    """Entry point from an opportunity's skill gap: claim the required skill if needed, then test it."""
    skill = get_object_or_404(Skill, pk=skill_id, is_active=True)
    ss = student_skill_for_test(request.student, skill)
    if ss is None:
        if async_tasks.wants_async(request):
            return JsonResponse({"error": "not_testable", "code": "not_testable", "message": str(NOT_TESTABLE)}, status=403)
        messages.warning(request, NOT_TESTABLE)
        return redirect("skills_hub")
    return challenge_start(request, ss.pk)


@student_gate()
@require_GET
def challenge_run(request, token):
    student = request.student
    attempt = get_object_or_404(ChallengeAttempt.objects.select_related("student_skill__skill"), token=token, student_skill__student=student)
    if not attempt.is_active:
        return redirect("skills_result", token=attempt.token)
    context = get_user_context(request)
    context.update({
        "attempt": attempt,
        "skill": attempt.student_skill.skill,
        # Strings the challenge script shows; %(n)s / %(max)s are filled in client-side.
        "js_i18n": {
            "easy": _("Easy"), "medium": _("Medium"), "hard": _("Hard"),
            "retrying": _("Connection problem. Retrying…"),
            "load_failed": _("Could not load the next question. Please refresh the page."),
            "focus_warning": _("Warning %(n)s of %(max)s: stay on this tab. The next time you leave, the session ends."),
        },
    })
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
        return JsonResponse({"error": "item_id required", "code": "invalid"}, status=400)
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
            return JsonResponse({"error": "unknown event", "code": "invalid"}, status=400)
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
        "leaderboard": engine.leaderboard_position(ss) if attempt.state == ChallengeAttempt.STATE_COMPLETED else None,
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
        return HttpResponseForbidden(_("You are not authorized to view this student's skill data."))
    context = get_user_context(request)
    from .evidence import evidence_rows

    context.update({
        "student": student,
        "viewer_role": role,
        "skills": student.skills.select_related("skill").order_by("-status", "-percentile", "skill__name"),
        "evidence": evidence_rows(student),
    })
    return render(request, "skills/report.html", context)


@login_required
def university_dashboard(request):
    partner = _get_partner_profile(request.user)
    if not is_verified_university(partner):
        return HttpResponseForbidden(_("Available to verified partner universities only."))
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
    per_student = Paginator(students.annotate(
        verified_count=Count("skills", filter=Q(skills__status=StudentSkill.STATUS_VERIFIED)),
        lag_count=Count("skills", filter=Q(skills__status=StudentSkill.STATUS_LAG)),
    ).order_by("-verified_count", "user__first_name", "pk"), UNIVERSITY_STUDENTS_PER_PAGE)
    per_student.count = total_students  # already counted above: skip the paginator's COUNT query
    students_page = per_student.get_page(request.GET.get("page"))
    context = get_user_context(request)
    context.update({
        "partner": partner,
        "total_students": total_students,
        "employable": employable,
        "employability_rate": round(employable / total_students * 100) if total_students else 0,
        "by_skill": by_skill,
        "students_page": students_page,
    })
    return render(request, "skills/university.html", context)
