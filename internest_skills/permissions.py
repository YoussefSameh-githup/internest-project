"""RBAC for skill badges / analytics: self, affiliated partner university, Pro employers."""
from internest_core.models import PartnerApplicantData, PartnerProfile, StudentProfile

ROLE_SELF = "self"
ROLE_UNIVERSITY = "university"
ROLE_EMPLOYER_PRO = "employer_pro"
ROLE_STAFF = "staff"


def _partner(user):
    try:
        return user.partnerprofile
    except PartnerProfile.DoesNotExist:
        return None


def _student(user):
    try:
        return user.studentprofile
    except StudentProfile.DoesNotExist:
        return None


def is_student_role(user) -> bool:
    """Any logged-in non-partner, non-admin account; its StudentProfile is created on first visit."""
    return user.is_authenticated and not user.is_superuser and _partner(user) is None


def student_for(user, create=False):
    """StudentProfile for a logged-in, non-partner user; None for startups, universities and anonymous."""
    if not user.is_authenticated or _partner(user) is not None:
        return None
    if create:
        return StudentProfile.objects.get_or_create(user=user)[0]
    return _student(user)


PROFILE_GATE_FIELDS = ("university", "major", "study_level")


def missing_profile_fields(student) -> list[str]:
    return [f for f in PROFILE_GATE_FIELDS if not str(getattr(student, f, None) or "").strip()]


def is_pro_employer(partner) -> bool:
    if partner is None or partner.is_academic:
        return False
    sub = getattr(partner, "subscription", None)  # RelatedObjectDoesNotExist is an AttributeError
    return bool(sub and sub.is_pro_active)


def is_verified_university(partner) -> bool:
    return bool(partner and partner.is_academic and partner.is_fully_verified)


def skill_view_role(user, student: StudentProfile):
    """Return the role granting access to `student`'s skill data, or None."""
    if not user.is_authenticated or student is None:
        return None
    if user.is_superuser:
        return ROLE_STAFF
    own = _student(user)
    if own is not None and own.pk == student.pk:
        return ROLE_SELF
    partner = _partner(user)
    if partner is None:
        return None
    if is_verified_university(partner):
        sp = getattr(student, "skill_profile", None)
        if sp is not None and sp.partner_university_id == partner.pk:
            return ROLE_UNIVERSITY
        return None
    if is_pro_employer(partner):
        # Pro employers see candidates that applied to them (forwarded by Internest).
        if PartnerApplicantData.objects.filter(partner=partner, student=student).exists():
            return ROLE_EMPLOYER_PRO
    return None
