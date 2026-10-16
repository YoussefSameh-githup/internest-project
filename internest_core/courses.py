"""Course completion → certificate → automatic skill verification.

Completion is confirmed by the course's own partner (or an Internest admin), never by the student, so a click
can't verify a skill. A completed enrollment is the certificate (public link: /courses/certificate/<uuid>/).
When the partner is accredited (fully verified by Internest) and the course teaches a skill, that skill becomes
Verified on the student's profile with no extra test.
"""
from django.db import transaction
from django.utils import timezone

from .models import StudentEnrollment


def is_accredited(course) -> bool:
    """Courses from Internest-verified partners (startups, academies, universities) that admins approved."""
    return course.status == "Approved" and bool(course.partner and course.partner.is_fully_verified)


def can_confirm_completion(user, enrollment) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    partner = getattr(user, "partnerprofile", None)
    return partner is not None and enrollment.course.partner_id == partner.pk


@transaction.atomic
def complete_enrollment(enrollment) -> bool:
    """Mark completed, issue the certificate and (accredited course + skill) verify the skill.

    Returns True when the skill was newly verified. Idempotent: completing twice changes nothing.
    """
    from internest_skills.models import StudentSkill

    enrollment = StudentEnrollment.objects.select_for_update().select_related("course__partner", "course__skill").get(pk=enrollment.pk)
    if enrollment.is_completed:
        return False
    enrollment.status, enrollment.completed_at = StudentEnrollment.STATUS_COMPLETED, timezone.now()
    enrollment.save(update_fields=["status", "completed_at"])

    course = enrollment.course
    if not (course.skill_id and is_accredited(course)):
        return False
    ss, _created = StudentSkill.objects.get_or_create(
        student=enrollment.student, skill=course.skill,
        defaults={"source": StudentSkill.SOURCE_EXPLICIT, "evidence": f"Course: {course.title}"[:300]},
    )
    if ss.status == StudentSkill.STATUS_VERIFIED:
        return False  # keep the earlier verification (and its challenge score)
    ss.status, ss.verified_via, ss.verified_at = StudentSkill.STATUS_VERIFIED, StudentSkill.VIA_COURSE, timezone.now()
    ss.save(update_fields=["status", "verified_via", "verified_at", "updated_at"])
    return True
