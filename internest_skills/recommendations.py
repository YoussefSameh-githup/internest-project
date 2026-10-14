"""Upskilling recommendations for detected skill lags.

Order for every skill: partner courses (Course.is_partner and approved partner submissions) → open
Course entries for the skill → curated external courses per sub-skill → external search links.
"""
from functools import reduce
from operator import or_
from urllib.parse import quote_plus

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from internest_core.models import PartnerCourseSubmission

from .models import Course, ExternalCourse, SubSkill

PARTNER_LIMIT_PER_GAP = 3
COURSE_LIMIT_PER_SKILL = 3
EXTERNAL_LIMIT_PER_GAP = 3

_EXTERNAL_SEARCH = [
    ("Coursera", "https://www.coursera.org/search?query={q}"),
    ("edX", "https://www.edx.org/search?q={q}"),
    ("Udemy", "https://www.udemy.com/courses/search/?q={q}"),
    ("YouTube", "https://www.youtube.com/results?search_query={q}"),
]


def _partner_courses(sub_skill=None, terms=None):
    terms = terms or sub_skill.search_terms
    match = reduce(or_, (Q(title__icontains=t) | Q(description__icontains=t) for t in terms))
    return (
        PartnerCourseSubmission.objects.filter(status="Approved")
        .filter(match)
        .select_related("partner")
        .order_by("-partner__is_academic", "-partner__is_fully_verified", "price")[:PARTNER_LIMIT_PER_GAP]
    )


def _course_rec(course, outcome):
    return {
        "sub_skill": None,
        "title": course.title,
        "provider": course.provider_name,
        "provider_type": "internest_partner" if course.is_partner else "external",
        "url": course.url,
        "price": None,
        "expected_outcome": outcome,
        "is_partner": course.is_partner,
        "discount_note": course.discount_note if course.is_partner else "",
        "opens_externally": True,
    }


def catalog_courses(skill, outcome, partner_only=False):
    """Course entries for a skill: partner courses when any exist, otherwise the open (internet) ones."""
    active = Course.objects.filter(skill=skill, is_active=True).order_by("-priority", "title")
    chosen = list(active.filter(is_partner=True)[:COURSE_LIMIT_PER_SKILL])
    if not chosen and not partner_only:
        chosen = list(active.filter(is_partner=False)[:COURSE_LIMIT_PER_SKILL])
    return [_course_rec(c, outcome) for c in chosen]


def _submission_rec(course, outcome, sub_skill=None):
    return {
        "sub_skill": sub_skill,
        "title": course.title,
        "provider": course.partner.company_name,
        "provider_type": "internest_partner",
        "url": reverse("course_checkout", args=[course.id]),
        "price": str(course.price),
        "expected_outcome": outcome,
        "is_partner": True,
        "discount_note": "",
        "opens_externally": False,
    }


def _level_up(student_skill):
    """Next-level courses for an already verified skill."""
    skill = student_skill.skill
    outcome = _("Advance your verified %(skill)s skill") % {"skill": skill.name}
    recs = catalog_courses(skill, outcome, partner_only=True) + [
        _submission_rec(c, outcome) for c in _partner_courses(terms=[skill.name] + [s.name for s in skill.sub_skills.all()])]
    if not recs:
        recs = catalog_courses(skill, outcome)
    if not recs:
        q = quote_plus(f"advanced {skill.name}")
        recs = [{
            "sub_skill": None,
            "title": _("Advanced %(skill)s on %(provider)s") % {"skill": skill.name, "provider": provider},
            "provider": provider,
            "provider_type": "external_search",
            "url": pattern.format(q=q),
            "price": None,
            "expected_outcome": _("Advance your verified %(skill)s skill") % {"skill": skill.name},
        } for provider, pattern in _EXTERNAL_SEARCH]
    return recs


def recommendations_for(student_skill) -> dict:
    gap_ids = [g["id"] for g in student_skill.lag_sub_skills] if student_skill.status == "lag" else []
    sub_skills = SubSkill.objects.filter(id__in=gap_ids, skill=student_skill.skill)
    recs = _level_up(student_skill) if student_skill.status == "verified" else []
    if sub_skills:
        skill = student_skill.skill
        gap_outcome = _("Close your %(skill)s gap and pass the retest") % {"skill": skill.name}
        # 1. Partner courses: the skill's partner catalogue entries + approved partner submissions per gap.
        partner = catalog_courses(skill, gap_outcome, partner_only=True)
        for sub in sub_skills:
            outcome = _("Close the gap in %(area)s and pass the %(skill)s retest") % {"area": sub.name, "skill": skill.name}
            partner += [_submission_rec(c, outcome, sub.name) for c in _partner_courses(sub)]
        # 2. No partner match: open (internet) courses mapped to the skill.
        chosen = partner or catalog_courses(skill, gap_outcome)
        if chosen:
            recs.extend(chosen)
            sub_skills = []  # 3–4 (curated per sub-skill, then search links) only when the catalogue has nothing
    for sub in sub_skills:
        outcome = _("Close the gap in %(area)s and pass the %(skill)s retest") % {"area": sub.name, "skill": student_skill.skill.name}
        curated = list(ExternalCourse.objects.filter(sub_skill=sub, is_active=True)[:EXTERNAL_LIMIT_PER_GAP])
        for c in curated:
            recs.append({
                "sub_skill": sub.name,
                "title": c.title,
                "provider": c.provider,
                "provider_type": "external",
                "url": c.url,
                "price": None,
                "expected_outcome": c.expected_outcome,
            })
        if not curated:
            q = quote_plus(f"{student_skill.skill.name} {sub.name}")
            for provider, pattern in _EXTERNAL_SEARCH:
                recs.append({
                    "sub_skill": sub.name,
                    "title": _("%(area)s courses on %(provider)s") % {"area": sub.name, "provider": provider},
                    "provider": provider,
                    "provider_type": "external_search",
                    "url": pattern.format(q=q),
                    "price": None,
                    "expected_outcome": outcome,
                })

    last = student_skill.attempts.filter(state="completed").order_by("-finished_at").first()
    return {
        "student_skill_id": student_skill.id,
        "score": student_skill.score,
        "percentile": student_skill.percentile,
        "improvement_topics": last.improvement_topics if last else [],
        "skill": student_skill.skill.name,
        "status": student_skill.status,
        "status_label": student_skill.status_label,
        "lag_sub_skills": student_skill.lag_sub_skills,
        "cooldown": {
            "retest_available_at": student_skill.cooldown_until.isoformat() if student_skill.cooldown_until else None,
            "seconds_remaining": student_skill.cooldown_seconds_remaining,
        },
        "recommendations": recs,
    }


def recommendations_for_student(student, limit=6) -> dict:
    """Courses suggested from the student's tested skills (skill gaps first, then level-ups).

    Internest partner courses are shown when any match; otherwise external search suggestions.
    """
    from .models import StudentSkill

    tested = (
        StudentSkill.objects.filter(student=student, status__in=[StudentSkill.STATUS_LAG, StudentSkill.STATUS_VERIFIED])
        .select_related("skill")
        .order_by("status")  # "lag" < "verified": skill gaps first
    )
    internal, external, seen = [], [], set()
    for ss in tested:
        for rec in recommendations_for(ss)["recommendations"]:
            if rec["url"] in seen:
                continue
            seen.add(rec["url"])
            rec = {**rec, "skill": ss.skill.name, "is_gap": ss.status == StudentSkill.STATUS_LAG}
            (internal if rec.get("is_partner") or rec["provider_type"] == "internest_partner" else external).append(rec)
    return {"internal": internal[:limit], "external": [] if internal else external[:limit], "tested": tested.exists()}


def track_recommendations(student_skill, limit=3) -> list[dict]:
    """Courses for one learning path: gap-specific first, else general courses for the skill."""
    return (recommendations_for(student_skill)["recommendations"] or _level_up(student_skill))[:limit]


def courses_for_skill(skill, limit=3) -> list[dict]:
    """Courses to close a missing skill: partner catalogue first, then open courses, then a search link."""
    outcome = _("Learn %(skill)s and prove it on Internest") % {"skill": skill.name}
    recs = catalog_courses(skill, outcome)
    if not recs:
        provider, pattern = _EXTERNAL_SEARCH[0]
        recs = [{"title": _("%(area)s courses on %(provider)s") % {"area": skill.name, "provider": provider},
                 "provider": provider, "provider_type": "external_search", "url": pattern.format(q=quote_plus(skill.name)),
                 "price": None, "expected_outcome": outcome, "is_partner": False, "discount_note": "", "opens_externally": True}]
    return recs[:limit]
