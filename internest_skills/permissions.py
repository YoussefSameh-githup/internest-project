"""RBAC for skill badges / analytics: self, affiliated partner university, Pro employers."""
from internest_core import identity
from internest_core.models import Application, PartnerProfile, StudentProfile

ROLE_SELF = "self"
ROLE_UNIVERSITY = "university"
ROLE_EMPLOYER_PRO = "employer_pro"
ROLE_STAFF = "staff"


def _partner(user):
    return identity.partner_profile(user)


def _student(user):
    return identity.student_profile(user)


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
    """Paid hiring access (Hiring Pass or legacy Pro): the rule lives in internest_startups.entitlements."""
    from internest_startups.entitlements import has_hiring_access

    return has_hiring_access(partner)


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
        # Paid employers see candidates who applied to an opportunity their access covers (legacy Pro: any of theirs).
        from internest_startups.entitlements import covered_internship_ids

        covered = covered_internship_ids(partner)
        applied = Application.objects.filter(internship__partner=partner, applicant_id=student.user_id)
        if covered is not None:
            applied = applied.filter(internship_id__in=covered)
        if applied.exists():
            return ROLE_EMPLOYER_PRO
    return None
