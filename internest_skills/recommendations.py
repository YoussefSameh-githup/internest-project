"""Upskilling recommendations for detected skill lags: Internest partners first, external fallback."""
from functools import reduce
from operator import or_
from urllib.parse import quote_plus

from django.db.models import Q
from django.urls import reverse

from internest_core.models import PartnerCourseSubmission

from .models import ExternalCourse, SubSkill

PARTNER_LIMIT_PER_GAP = 3
EXTERNAL_LIMIT_PER_GAP = 3

_EXTERNAL_SEARCH = [
    ("Coursera", "https://www.coursera.org/search?query={q}"),
    ("edX", "https://www.edx.org/search?q={q}"),
    ("Udemy", "https://www.udemy.com/courses/search/?q={q}"),
    ("YouTube", "https://www.youtube.com/results?search_query={q}"),
]


def _partner_courses(sub_skill):
    terms = sub_skill.search_terms
    match = reduce(or_, (Q(title__icontains=t) | Q(description__icontains=t) for t in terms))
    return (
        PartnerCourseSubmission.objects.filter(status="Approved")
        .filter(match)
        .select_related("partner")
        .order_by("-partner__is_academic", "-partner__is_fully_verified", "price")[:PARTNER_LIMIT_PER_GAP]
    )


def recommendations_for(student_skill) -> dict:
    gap_ids = [g["id"] for g in student_skill.lag_sub_skills]
    sub_skills = SubSkill.objects.filter(id__in=gap_ids, skill=student_skill.skill)
    recs = []
    for sub in sub_skills:
        outcome = f"Close the gap in {sub.name} and pass the {student_skill.skill.name} retest"
        partner = list(_partner_courses(sub))
        for c in partner:
            recs.append({
                "sub_skill": sub.name,
                "title": c.title,
                "provider": c.partner.company_name,
                "provider_type": "internest_partner",
                "url": reverse("course_checkout", args=[c.id]),
                "price": str(c.price),
                "expected_outcome": outcome,
            })
        if partner:
            continue
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
                    "title": f"{sub.name} courses on {provider}",
                    "provider": provider,
                    "provider_type": "external_search",
                    "url": pattern.format(q=q),
                    "price": None,
                    "expected_outcome": outcome,
                })

    return {
        "student_skill_id": student_skill.id,
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
