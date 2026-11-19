"""Facts for the public "Why trust Internest?" page (Phase 3).

Every number on that page is computed here from the live database or read from the constants the code enforces, so
the page cannot drift from what the product really does. Nothing is typed in by hand except team profiles and stories,
which staff add in admin and which only appear once published (stories also need recorded consent).
"""
from django.db.models import Count, Q

from .page_cache import cached_value

TTL = 600  # counts refresh every 10 minutes; the page is also cached for anonymous visitors


def rules() -> dict:
    """The assessment rules as the code enforces them (shown on the page, never hard-coded in the template)."""
    from internest_skills.models import RETEST_COOLDOWN, ChallengeAttempt, ChallengeItem
    from internest_skills.review import FAST_ANSWER_MS, SIMILARITY_THRESHOLD

    low, high = ChallengeItem.QUICK_SECONDS
    practical_minutes = sorted(
        {s // 60 for s in ChallengeItem.objects.filter(kind=ChallengeItem.KIND_PRACTICAL, is_active=True)
         .values_list("time_limit_seconds", flat=True)}
    )
    from django.conf import settings

    return {
        "freelance_auto": bool(getattr(settings, "FREELANCE_AUTO_VERIFY", False)),
        "question_seconds": f"{low}–{high}",
        "practical_minutes": (None if not practical_minutes else str(practical_minutes[0]) if len(practical_minutes) == 1
                              else f"{practical_minutes[0]}–{practical_minutes[-1]}"),
        "focus_limit": ChallengeAttempt.MAX_FOCUS_WARNINGS,
        "cooldown_hours": int(RETEST_COOLDOWN.total_seconds() // 3600),
        "fast_seconds": FAST_ANSWER_MS // 1000,
        "similarity_percent": round(SIMILARITY_THRESHOLD * 100),
        "review_days": 3,  # the target shown to students on the result page (not measured)
    }


def _numbers() -> dict:
    from internest_skills.models import ChallengeItem, Skill
    from internest_skills.verification import pool_stats

    from .models import Application, GigExperience, PartnerProfile

    stats = pool_stats()
    partners = PartnerProfile.objects.filter(is_fully_verified=True, user__is_active=True).aggregate(
        companies=Count("pk", filter=Q(is_academic=False)), universities=Count("pk", filter=Q(is_academic=True)))
    # Distinct real candidates, at companies (not universities), active accounts only.
    company_apps = Application.objects.filter(internship__partner__is_academic=False, applicant__is_active=True)
    apps = company_apps.aggregate(
        shortlisted=Count("applicant", distinct=True, filter=Q(shortlisted_at__isnull=False)),
        accepted=Count("applicant", distinct=True, filter=Q(status__in=(Application.STATUS_ACCEPTED, Application.STATUS_COMPLETED))))
    # A test can start only with at least 3 active questions (challenge._pick_items); practical tasks don't count.
    tested = (Skill.objects.annotate(n=Count("items", filter=Q(items__is_active=True) & ~Q(items__kind=ChallengeItem.KIND_PRACTICAL)))
              .filter(n__gte=3))
    return {
        "candidates": stats["candidates"],
        "verified_skills": stats["verified_skills"],
        "skills_with_tests": tested.count(),
        "practical_skills": tested.filter(items__kind=ChallengeItem.KIND_PRACTICAL, items__is_active=True).distinct().count(),
        "practical_tasks": ChallengeItem.objects.filter(kind=ChallengeItem.KIND_PRACTICAL, is_active=True).count(),
        "verified_companies": partners["companies"],
        "verified_universities": partners["universities"],
        "shortlisted": apps["shortlisted"],
        "accepted": apps["accepted"],
        "verified_gigs": GigExperience.objects.verified().count(),
    }


def numbers() -> dict:
    return cached_value("trust", "numbers", TTL, _numbers)


def team():
    from .models import TeamMember

    return list(TeamMember.objects.filter(is_published=True))


def stories():
    from .models import TrustStory

    published = TrustStory.objects.filter(is_published=True, consent_given_at__isnull=False)
    return {"testimonials": list(published.filter(kind=TrustStory.KIND_TESTIMONIAL)[:6]),
            "case_studies": list(published.filter(kind=TrustStory.KIND_CASE_STUDY)[:4])}
