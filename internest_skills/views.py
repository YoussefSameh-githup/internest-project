import json
import logging
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _, gettext_lazy as _lazy
from django.views.decorators.http import require_GET, require_POST

from internest_core.models import StudentProfile
from internest_core.views import _get_partner_profile, get_user_context

from . import challenge as engine
from .extraction import ExtractionError, analyze_cv, read_document_text, sanitize_text, save_claimed_skills
from . import freelance_service
from .forms import CertificateForm, FreelanceImportForm, GigProofForm, SkillSourceForm, UniversityShareForm
from .llm import llm_enabled
from .models import ChallengeAttempt, ChallengeItem, Discipline, Skill, SkillProfile, StudentFreelanceGig, StudentSkill
from .permissions import is_verified_university, missing_profile_fields, skill_view_role, student_for
from .quizgen import quiz_item_ids_for
from .recommendations import recommendations_for

logger = logging.getLogger(__name__)

MAX_EXTRACTIONS_PER_DAY = 5
MAX_FREELANCE_IMPORTS_PER_DAY = 5
OWNERSHIP_WARNING = _lazy(
    "Note: the username in the platform link doesn't directly match the account owner's name. Please upload a "
    "screenshot that shows you own the account to speed up verification.")
AI_NOTICE_SESSION_KEY = "skills_ai_notice"  # shown inline on the hub after an analysis where every AI key failed


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
    skill_profile, _created = SkillProfile.objects.get_or_create(student=student)

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
        "verified": [s for s in skills if s.status == StudentSkill.STATUS_VERIFIED],
        "lagging": [s for s in skills if s.status == StudentSkill.STATUS_LAG],
        "claimed": [s for s in skills if s.status == StudentSkill.STATUS_CLAIMED],
        "can_generate_quiz": llm_enabled(),
        "ai_notice": request.session.pop(AI_NOTICE_SESSION_KEY, None),
        "top_domains": [Discipline(d).label for d in skill_profile.top_domains if d in Discipline.values],
    })
    return render(request, "skills/hub.html", context)


def _handle_extraction(request, student, skill_profile, data):
    today = timezone.localdate()
    if skill_profile.extraction_day != today:
        skill_profile.extraction_day = today
        skill_profile.extraction_count_today = 0

    if data.get("linkedin_url"):
        skill_profile.linkedin_url = data["linkedin_url"]

    files = [f for f in (data.get("cv_file"), data.get("linkedin_pdf")) if f]
    pasted = data.get("pasted_text") or ""
    if (files or pasted) and skill_profile.extraction_count_today >= MAX_EXTRACTIONS_PER_DAY:
        skill_profile.save()
        messages.error(request, _("Daily analysis limit reached. Try again tomorrow."))
        return

    if files or pasted:
        try:
            parts = [read_document_text(f) for f in files] + ([sanitize_text(pasted)] if pasted else [])
        except ExtractionError as exc:
            skill_profile.save()
            messages.error(request, str(exc))
            return
        text = "\n\n".join(parts)
        analysis = analyze_cv(text)
        extracted = analysis.skills
        if analysis.ai_failed:
            # Every AI key failed: keep what the local matcher found, say so inline, and don't use up a daily try.
            request.session[AI_NOTICE_SESSION_KEY] = "partial" if extracted else "empty"
            if not extracted:
                skill_profile.save()
                return
        else:
            skill_profile.extraction_count_today += 1
        if analysis.ai_used:
            skill_profile.cv_summary = analysis.summary
            skill_profile.top_domains = analysis.top_domains
        created, updated = save_claimed_skills(student, extracted, analysis.years_by_skill)
        skill_profile.last_extracted_at = timezone.now()
        explicit = sum(1 for e in extracted if e.source == StudentSkill.SOURCE_EXPLICIT)
        messages.success(
            request,
            _("Analysis complete: %(explicit)s explicit and %(inferred)s inferred skills found (%(new)s new). "
              "They are marked as Claimed until you verify them.")
            % {"explicit": explicit, "inferred": len(extracted) - explicit, "new": created},
        )
    else:
        messages.info(
            request,
            _("LinkedIn URL saved. To analyze your skills, upload your CV or LinkedIn PDF, or paste your "
              "About, Experience and Skills text."),
        )
    skill_profile.save()


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
    return redirect(f"{reverse('profile')}?tab=portfolio")


@student_gate()
@require_POST
def skills_share_university(request):
    form = UniversityShareForm(request.POST)
    if form.is_valid():
        skill_profile, _created = SkillProfile.objects.get_or_create(student=request.student)
        skill_profile.partner_university = form.cleaned_data["partner_university"]
        skill_profile.save(update_fields=["partner_university"])
        messages.success(request, _("Your sharing preference was saved."))
    return redirect(f"{reverse('profile')}?tab=settings")


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
@student_gate()
@require_POST
def challenge_start(request, student_skill_id):
    student = request.student
    ss = get_object_or_404(StudentSkill.objects.select_related("skill"), pk=student_skill_id, student=student)
    item_ids = None
    # Live quiz generation (network) happens outside the row lock; skipped when resuming or not eligible.
    can_start = ss.status != StudentSkill.STATUS_VERIFIED and not ss.in_cooldown
    if can_start and not ss.attempts.filter(state=ChallengeAttempt.STATE_ACTIVE).exists():
        try:
            item_ids = quiz_item_ids_for(ss.skill)  # AI quiz, else the saved bank
        except Exception:
            logger.exception("Preparing a quiz failed for skill %s", ss.skill_id)
            item_ids = []
        if not item_ids:
            return _quiz_unavailable(request, ss)
    try:
        with transaction.atomic():
            ss = get_object_or_404(StudentSkill.objects.select_for_update().select_related("skill"), pk=student_skill_id, student=student)
            attempt = engine.start_attempt(ss, item_ids=item_ids)
    except engine.ChallengeError as exc:  # cooldown, already verified, abandoned session
        messages.warning(request, str(exc))
        return redirect("skills_hub")
    except Http404:
        raise
    except Exception:
        logger.exception("Starting a challenge failed for student skill %s", student_skill_id)
        return _quiz_unavailable(request, ss)
    return redirect("skills_challenge", token=attempt.token)


def _quiz_unavailable(request, student_skill):
    """Friendly 200 page when neither the AI nor the saved bank can supply a quiz."""
    context = get_user_context(request)
    context.update({
        "skill": student_skill.skill,
        "message": _("Not enough questions are available for this challenge right now. Please try again later."),
    })
    return render(request, "skills/challenge_unavailable.html", context)


@student_gate()
@require_POST
def challenge_start_for_skill(request, skill_id):
    """Entry point from an opportunity's skill gap: claim the required skill if needed, then test it."""
    skill = get_object_or_404(Skill, pk=skill_id, is_active=True)
    ss, _created = StudentSkill.objects.get_or_create(
        student=request.student, skill=skill,
        defaults={"source": StudentSkill.SOURCE_EXPLICIT, "evidence": ""},
    )
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
