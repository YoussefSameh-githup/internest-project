"""Talent Pool: opted-in students with verified skills (or a quiz score ≥ 80%), searchable by Pro startups."""
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.db.models import Max, Prefetch, Q
from django.template.loader import render_to_string
from django.utils import timezone

from internest_core.models import StudentProfile
from internest_skills.models import Skill, StudentSkill

TOP_SCORE = 80
DAILY_INVITATIONS = 20  # per startup, anti-spam
INVITES_PER_OPPORTUNITY = 3  # invited students skip the skill gate, so keep it scarce


def valid_invitation(user, internship, token):
    """The invitation behind `?invite_token=` if it belongs to this student and opportunity, else None."""
    import uuid

    from .models import TalentInvitation

    if not token or not getattr(user, "is_authenticated", False):
        return None
    try:
        token = uuid.UUID(str(token))
    except ValueError:
        return None
    return TalentInvitation.objects.filter(token=token, internship=internship, student__user=user).first()


def qualifying_skills():
    return StudentSkill.objects.filter(Q(status=StudentSkill.STATUS_VERIFIED) | Q(score__gte=TOP_SCORE))


def pool_queryset(params):
    """Opted-in candidates matching the GET filters, best score first, with their qualifying skills attached."""
    skills = qualifying_skills()
    if params.get("skill", "").isdigit():
        skills = skills.filter(skill_id=int(params["skill"]))
    if params.get("min_score", "").isdigit():
        skills = skills.filter(score__gte=min(int(params["min_score"]), 100))
    students = StudentProfile.objects.filter(talent_pool_visible=True, pk__in=skills.values("student_id"))

    q = params.get("q", "").strip()[:80]
    if q:
        students = students.filter(
            Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(major__icontains=q)
            | Q(university__icontains=q) | Q(bio__icontains=q)
            | Q(pk__in=qualifying_skills().filter(skill__name__icontains=q).values("student_id"))
        )
    if params.get("university"):
        students = students.filter(university=params["university"])  # exact: indexable (values come from the dropdown)
    if params.get("major"):
        students = students.filter(major=params["major"])

    shown = qualifying_skills().select_related("skill").order_by("-score", "skill__name")
    return (
        students.select_related("user")
        .annotate(best_score=Max("skills__score", filter=Q(skills__in=qualifying_skills())))
        .prefetch_related(Prefetch("skills", queryset=shown, to_attr="pool_skills"))
        .order_by("-best_score", "-pk")
    )


def filter_options():
    pool = StudentProfile.objects.filter(talent_pool_visible=True, pk__in=qualifying_skills().values("student_id"))

    def distinct(field):  # stored values as-is, so the exact-match filter finds them
        return sorted({v for v in pool.exclude(**{f"{field}__isnull": True}).values_list(field, flat=True) if v and v.strip()},
                      key=str.lower)

    return {
        "skills": Skill.objects.filter(student_skills__in=qualifying_skills(), student_skills__student__in=pool).distinct().order_by("name"),
        "universities": distinct("university"),
        "majors": distinct("major"),
    }


def invitations_today(partner) -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return partner.talent_invitations.filter(created_at__gte=start).count()


def send_invitation_email(invitation, detail_url) -> bool:
    student = invitation.student
    to = student.personal_email or student.user.email
    if not to:
        return False
    context = {
        "name": student.user.get_full_name() or student.user.username,
        "company": invitation.partner.company_name,
        "title": invitation.internship.title,
        "message": invitation.message,
        "url": detail_url,
    }
    try:
        return bool(EmailMultiAlternatives(
            subject=f"✉️ دعوة للتقديم من {context['company']} | Invitation to apply — {context['title']}",
            body=render_to_string("emails/talent_invitation.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[to],
        ).send(fail_silently=False))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to send talent invitation %s", invitation.pk)
        return False
