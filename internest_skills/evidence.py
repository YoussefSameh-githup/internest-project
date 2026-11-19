"""Candidate evidence report (Phase 3): for each verified skill, how it was verified and what backs it.

Shown only where the existing report access rules allow (skill_view_role: the student, the university they share
with, a company whose paid access covers an opportunity the student applied to, superusers). Built from stored records.
"""
from django.db.models import Prefetch

from .models import ChallengeAttempt, ChallengeItem, StudentSkill


def evidence_rows(student):
    """[{skill, via, verified_at, score, percentile, practical, human_reviewed, reviewed_at}] for verified skills, best first."""
    completed = ChallengeAttempt.objects.filter(state=ChallengeAttempt.STATE_COMPLETED).order_by("-finished_at")
    skills = (StudentSkill.objects.filter(student=student, status=StudentSkill.STATUS_VERIFIED).select_related("skill")
              .prefetch_related(Prefetch("attempts", queryset=completed, to_attr="completed_attempts"))
              .order_by("-percentile", "skill__name"))
    practical_ids = set(ChallengeItem.objects.filter(kind=ChallengeItem.KIND_PRACTICAL).values_list("id", flat=True))
    from django.conf import settings
    from django.utils.translation import gettext as _

    rows = []
    for ss in skills:
        attempt = None
        if ss.verified_via in ("", StudentSkill.VIA_CHALLENGE):
            attempt = next((a for a in ss.completed_attempts if a.review_status in ("", ChallengeAttempt.REVIEW_APPROVED)), None)
        by_test = attempt is not None
        if ss.verified_via:
            via = ss.get_verified_via_display()
        else:  # older challenge verifications predate verified_via; anything else was set by hand
            via = StudentSkill(verified_via=StudentSkill.VIA_CHALLENGE).get_verified_via_display() if by_test else _("Set by our team")
        rows.append({
            "skill": ss.skill.name,
            "via": via,
            "verified_at": ss.verified_at,
            "score": ss.score if by_test else None,  # a failed test before a certificate is not evidence for it
            "percentile": ss.percentile if by_test else None,
            "practical": bool(by_test and practical_ids & set(attempt.item_ids or [])),
            "human_reviewed": (by_test and attempt.review_status == ChallengeAttempt.REVIEW_APPROVED)
                              or ss.verified_via == StudentSkill.VIA_CERTIFICATE
                              or (ss.verified_via == StudentSkill.VIA_FREELANCE and not settings.FREELANCE_AUTO_VERIFY),
            "reviewed_at": attempt.reviewed_at if by_test else None,
        })
    return rows
