"""Student Hub data: everything the profile, applications, tasks and courses pages show about a student.

Views stay thin; every query that more than one page needs lives here once. Optional tables (skills, CV, campus)
must never take a page down, so each of those lookups is wrapped by `db_safe`.
"""
import logging
from functools import wraps

from django.db import DatabaseError, transaction

from .models import (
    Application, Internship, StudentEnrollment, TOP_VERIFIED_MIN_GIGS, proof_of_work,
)

logger = logging.getLogger(__name__)

TABS = ("overview", "skills", "activity", "edit")
DEFAULT_TAB = TABS[0]
# Old bookmarks and redirects (?tab=details / portfolio / settings) keep working.
LEGACY_TABS = {"details": "edit", "settings": "edit", "portfolio": "activity"}

# Profile form fields per accordion section of the "Edit profile" tab.
FORM_SECTIONS = {
    "contact": ("personal_email", "university_email", "phone_number", "university", "major", "study_level"),
    "about": ("bio", "linkedin_url", "portfolio_url"),
    "files": ("profile_picture", "cv_file"),
}


def db_safe(default):
    """On a database error log it and return `default` (a value, or a factory for mutable ones).

    Inside a transaction the call runs in a savepoint so a failed query cannot poison the outer transaction
    (PostgreSQL). In autocommit mode (normal requests) no savepoint is needed: that saves two statements per call."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                if transaction.get_connection().in_atomic_block:
                    with transaction.atomic():
                        return fn(*args, **kwargs)
                return fn(*args, **kwargs)
            except DatabaseError:
                logger.exception("Student Hub data unavailable: %s", fn.__name__)
                return default() if callable(default) else default
        return wrapper
    return decorator


# ---------------------------------------------------------------- tabs and form sections
def resolve_tab(requested, has_errors=False):
    """The tab to open: form errors always show the edit tab, unknown names fall back to the overview."""
    if has_errors:
        return "edit"
    tab = LEGACY_TABS.get(requested, requested)
    return tab if tab in TABS else DEFAULT_TAB


def open_sections(form):
    """Accordion sections that start expanded: the first one, plus any section holding a field error."""
    opened = {"contact"}
    for section, fields in FORM_SECTIONS.items():
        if any(name in form.errors for name in fields):
            opened.add(section)
    return opened


# ---------------------------------------------------------------- skills
@db_safe(list)
def student_skills(profile):
    """All of the student's skills with their skill rows, verified first then alphabetical."""
    from internest_skills.models import StudentSkill

    skills = list(profile.skills.select_related("skill").order_by("skill__name"))
    return sorted(skills, key=lambda s: s.status != StudentSkill.STATUS_VERIFIED)


def group_skills(skills):
    """Split into verified skills and the rest ("to verify"), the rest tagged by where the skill came from."""
    from internest_skills.models import StudentSkill

    verified = [s for s in skills if s.status == StudentSkill.STATUS_VERIFIED]
    unverified = [s for s in skills if s.status != StudentSkill.STATUS_VERIFIED]
    return {
        "verified": verified,
        "unverified": unverified,
        "explicit": [s for s in unverified if s.source == StudentSkill.SOURCE_EXPLICIT],
        "inferred": [s for s in unverified if s.source != StudentSkill.SOURCE_EXPLICIT],
    }


@db_safe(0)
def verified_skills_count(profile):
    return profile.skills.filter(status="verified").count()


@db_safe(list)
def skill_quizzes(profile):
    """The student's skill tests (claimed → start, lag → retake, verified → done)."""
    return list(profile.skills.select_related("skill").order_by("status", "skill__name"))


@db_safe(False)
def talent_pool_eligible(profile):
    """Has a verified skill (one rule for the pool, its counts and this hint: `internest_skills.verification`)."""
    from internest_skills.verification import is_pool_eligible

    return is_pool_eligible(profile)


# ---------------------------------------------------------------- applications (shared with "My applications")
def student_applications(user):
    return (Application.objects.filter(applicant=user)
            .select_related("internship", "internship__partner", "experience", "internship__screening_task", "screening_submission")
            .order_by("-application_date"))


def open_invitations(user, applied_ids, limit=10):
    """Talent Pool invitations to opportunities that are still open and not applied to yet."""
    from internest_startups.models import TalentInvitation

    return [
        inv for inv in TalentInvitation.objects.filter(student__user=user, internship__in=Internship.objects.open())
        .select_related("internship", "partner")[:limit]
        if inv.internship_id not in applied_ids
    ]


# ---------------------------------------------------------------- campus + header stats
@db_safe(None)
def campus_share(request, profile):
    """Share link + text when the student already voted for their campus, else None."""
    from internest_campus.models import CampusDemandVote
    from internest_campus.views import _share_payload

    vote = CampusDemandVote.objects.filter(student=profile).first()
    return _share_payload(request, vote) if vote else None


def skills_share(request, verified):
    """Text + link for sharing verified skills. The link is the public landing page: profiles are private by design."""
    from django.urls import reverse
    from django.utils.translation import gettext as _

    if not verified:
        return None
    names = ", ".join(s.skill.name for s in verified[:4])
    return {"url": request.build_absolute_uri(reverse("landing")),
            "text": _("I'm AI-verified in %(skills)s on Internest. Verify your skills and meet startups hiring Egyptian students:") % {"skills": names}}


def completed_courses(profile):
    return StudentEnrollment.objects.filter(student=profile, status=StudentEnrollment.STATUS_COMPLETED).count()


def hub_header(request, profile, tab):
    """Header + tabs shared by My applications, Tasks & quizzes and Courses."""
    return {
        "hub_tab": tab,
        "hub_stats": {
            "applications": Application.objects.filter(applicant=request.user).count(),
            "verified": verified_skills_count(profile),
            "certificates": completed_courses(profile),
        },
    }


# ---------------------------------------------------------------- profile page
def missing_fields(profile):
    from django.utils.translation import gettext as _

    return [label for done, label in (
        (profile.university or profile.major, _("University & major")), (profile.study_level, _("Study level")),
        (profile.cv_file, _("CV file")), (profile.profile_picture, _("Profile picture")),
        ((profile.linkedin_url or "").startswith("http"), _("LinkedIn URL")),
    ) if not done]


@db_safe(dict)
def _portfolio(profile):
    gigs = list(profile.freelance_gigs.prefetch_related("skills"))
    return {
        "gigs": gigs,
        "needs_proof": sum(1 for g in gigs if g.status == g.STATUS_NEEDS_PROOF),
        "projects": list(profile.projects.all()),
    }


def sync_profile_education(profile, education):
    """CV Builder education is the single source of truth: copy its primary entry to StudentProfile.university / major.

    Returns True when the profile changed. A CV without a usable education entry never blanks the profile."""
    from internest_students.cv_builder_service import primary_education

    primary = primary_education(education)
    if not primary:
        return False
    profile.refresh_from_db()  # never save a stale copy over concurrent edits
    university, major = primary.get("university", ""), primary.get("major", "")
    if (profile.university, profile.major) == (university, major):
        return False
    profile.university, profile.major = university, major
    profile.calculate_completion()  # saves; university/major count towards the readiness score
    return True


_NO_CV = object()


def cv_record(profile):
    """The student's StudentCV row (or None), read once per request and memoised on the profile instance."""
    from internest_students.models import StudentCV

    record = getattr(profile, "_internest_cv", _NO_CV)
    if record is _NO_CV:
        record = StudentCV.objects.filter(student=profile).first() if profile.pk else None
        profile._internest_cv = record
    return record


@db_safe(None)
def cv_primary_education(profile):
    """The student's primary CV Builder education entry (dict) or None."""
    from internest_students.cv_builder_service import primary_education

    record = cv_record(profile)
    return primary_education((record.data or {}).get("education")) if record else None


@db_safe(dict)
def _cv_details(profile):
    """The saved CV record, its education timeline, expected graduation and a GitHub link."""
    from internest_students.cv_builder_service import clean_url, format_period, primary_education

    record = cv_record(profile)
    record = record if record is not None and record.data else None
    cv = record.data if record else {}
    out = {"cv": record, "education": [], "expected_graduation": "", "graduation_expected": False, "github_url": ""}
    for e in cv.get("education") or []:
        out["education"].append({**e, "period": format_period(e.get("start_date", ""), e.get("end_date", ""), e.get("is_current", False))})
    graduation = primary_education(cv.get("education"))
    if graduation and (graduation.get("end_date") or graduation.get("is_current")):
        if graduation.get("is_current"):
            out["expected_graduation"] = "Present"
        else:
            period = format_period("", graduation["end_date"])
            out["expected_graduation"] = period.replace(" (Expected)", "")
            out["graduation_expected"] = period.endswith("(Expected)")
    for url in (profile.portfolio_url, cv.get("github")):
        if url and "github.com" in url:
            out["github_url"] = clean_url(url).href_url
            break
    return out


@db_safe(dict)
def _skill_forms(profile):
    from internest_skills.forms import SkillSourceForm, UniversityShareForm
    from internest_skills.models import SkillProfile

    skill_profile = SkillProfile.objects.filter(student=profile).first()
    return {
        "analyze_form": SkillSourceForm(),
        "share_form": UniversityShareForm(initial={"partner_university": skill_profile.partner_university_id if skill_profile else None}),
    }


def profile_context(request, profile, form):
    """Everything profile.html renders. Each optional lookup degrades to an empty value instead of failing the page."""
    from internest_skills.forms import SkillSourceForm

    skills = student_skills(profile) if profile.pk else []
    groups = group_skills(skills)
    ctx = {
        "profile": profile, "form": form, "open_sections": open_sections(form),
        "active_tab": resolve_tab(request.GET.get("tab"), has_errors=bool(form.errors)),
        "missing": missing_fields(profile), "skill_groups": groups, "verified_skills_count": len(groups["verified"]),
        "skills_share": skills_share(request, groups["verified"]),
        "talent_pool_eligible": talent_pool_eligible(profile), "campus_share": campus_share(request, profile), "top_min": TOP_VERIFIED_MIN_GIGS,
        "proof": proof_of_work(profile) if profile.pk else None,
        "experiences": profile.gig_experiences.verified().select_related("company") if profile.pk else [],
        "gigs": [], "projects": [], "needs_proof": 0, "cv": None, "education": [], "expected_graduation": "",
        "graduation_expected": False, "github_url": "", "analyze_form": SkillSourceForm(), "share_form": None,
    }
    if profile.pk:
        ctx.update(_portfolio(profile))
        ctx.update(_cv_details(profile))
        ctx.update(_skill_forms(profile))
    # Out of sync (CV edited elsewhere, old profile row): show the CV's degree, not the stale profile fields.
    from internest_students.cv_builder_service import primary_education

    primary = primary_education(ctx["education"]) or {}
    ctx.update({"education_locked": bool(primary), "display_university": primary.get("university") or profile.university,
                "display_major": primary.get("major") or profile.major})
    return ctx
