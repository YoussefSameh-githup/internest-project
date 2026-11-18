"""One definition of a "verified candidate" for every Talent Pool count, search, badge and preview (audit H-1).

A skill counts only when its status is `verified`: a passed challenge with no lagging sub-skill, reviewed freelance
work, a reviewed certificate or an accredited course. A high score alone never counts, because a student can score
70%+ and still fail on a lagging sub-skill. A candidate counts only when the student opted in to the Talent Pool and
the account is active.

Anonymous previews return plain dicts (never model instances or primary keys), so a template cannot leak a name,
university, photo, contact or an id that could be matched against the paid search.
"""
from django.db.models import Count, F, Max, Prefetch, Q
from django.utils.translation import gettext as _

from .models import StudentSkill

PREVIEW_LIMIT = 6
MIN_FILTER_GROUP = 3  # a skill held by fewer candidates is counted but not filterable: one card would be one person
MAJOR_MAX = 40  # major is free text; long values can carry a name or a year
SCORE_BAND = 10  # previews show "80–89%", not the exact score, so a card is harder to match to a person


def verified_skills():
    """Verified skills of active accounts (opted in or not): the base of every rule below."""
    return StudentSkill.objects.filter(status=StudentSkill.STATUS_VERIFIED, student__user__is_active=True)


def pool_skills():
    """Verified skills of candidates who opted in to the Talent Pool."""
    return verified_skills().filter(student__talent_pool_visible=True)


def pool_candidates():
    """Opted-in, active students with at least one verified skill."""
    from internest_core.models import StudentProfile

    return StudentProfile.objects.filter(talent_pool_visible=True, user__is_active=True,
                                         pk__in=verified_skills().values("student_id"))


def parse_id(value):
    """A positive int from a query parameter, else None (ASCII digits only: "²".isdigit() is True but int() fails)."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and 0 < len(value) <= 9 else None


def is_pool_eligible(profile) -> bool:
    """True if the student has a verified skill (opt-in is checked separately, so the profile can explain it)."""
    return verified_skills().filter(student=profile).exists()


def _via_labels():
    return dict(StudentSkill.VIA_CHOICES)


def pool_stats():
    """Real, current numbers for employers before they pay: candidates, per skill, per verification method."""
    skills = pool_skills()
    labels = _via_labels()
    per_skill = list(
        skills.values("skill_id", "skill__name").annotate(candidates=Count("student_id", distinct=True))
        .order_by("-candidates", "skill__name")
    )
    per_method = [
        {"via": row["verified_via"], "label": labels.get(row["verified_via"], _("Other review")), "skills": row["n"]}
        for row in skills.values("verified_via").annotate(n=Count("id")).order_by("-n", "verified_via")
    ]
    return {
        "candidates": pool_candidates().count(),
        "verified_skills": skills.count(),
        "per_skill": [{"id": r["skill_id"], "name": r["skill__name"], "candidates": r["candidates"],
                       "filterable": r["candidates"] >= MIN_FILTER_GROUP} for r in per_skill],
        "per_method": per_method,
    }


def _band(score):
    if score is None:
        return None
    low = min(score // SCORE_BAND * SCORE_BAND, 100 - SCORE_BAND)
    return f"{low}–{low + SCORE_BAND - 1}%" if low + SCORE_BAND < 100 else f"{low}–100%"


def _short(text):
    text = " ".join((text or "").split())
    return text if len(text) <= MAJOR_MAX else text[:MAJOR_MAX - 1].rstrip() + "…"


def anonymous_previews(skill_id=None, limit=PREVIEW_LIMIT):
    """Up to `limit` anonymised candidate summaries: study field, study year and verified skills only."""
    candidates = pool_candidates()
    if skill_id:
        candidates = candidates.filter(pk__in=pool_skills().filter(skill_id=skill_id).values("student_id"))
    shown = verified_skills().select_related("skill").order_by("-score", "skill__name")
    rows = (
        candidates.annotate(best=Max("skills__score", filter=Q(skills__status=StudentSkill.STATUS_VERIFIED)))
        .prefetch_related(Prefetch("skills", queryset=shown, to_attr="verified_list"))
        .order_by(F("best").desc(nulls_last=True), "-pk")[:limit]  # certificate/course-only (no score) last on every DB
    )
    labels = _via_labels()
    return [
        {
            "major": _short(c.major),
            "study_level": c.get_study_level_display() if c.study_level else "",
            "skills": [{"name": s.skill.name, "band": _band(s.score), "via": labels.get(s.verified_via, _("Other review"))}
                       for s in c.verified_list],
        }
        for c in rows
    ]
