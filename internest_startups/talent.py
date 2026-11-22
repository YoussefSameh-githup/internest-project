"""Talent Pool: opted-in students with verified skills, searchable by companies with paid access (Hiring Pass or legacy Pro).

Who counts as a candidate is defined once, in `internest_skills.verification` (a high score alone never counts).
"""
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.db.models import Avg, Case, Count, F, IntegerField, Max, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.template.loader import render_to_string
from django.utils import timezone

from internest_core.models import TOP_VERIFIED_MIN_GIGS, GigExperience
from internest_skills.models import Skill
from internest_skills.verification import parse_id, pool_candidates, verified_skills

from internest_skills.matching import MATCH_THRESHOLD

TOP_SCORE = MATCH_THRESHOLD  # lowest step of the min-score filter only; eligibility is `verification.verified_skills`
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
    return verified_skills()


def pool_queryset(params):
    """Opted-in candidates matching the GET filters, best score first, with their qualifying skills attached."""
    skills = qualifying_skills()
    skill_id, min_score = parse_id(params.get("skill")), parse_id(params.get("min_score"))
    if skill_id:
        skills = skills.filter(skill_id=skill_id)
    if min_score:
        skills = skills.filter(score__gte=min(min_score, 100))
    students = pool_candidates().filter(pk__in=skills.values("student_id"))

    q = params.get("q", "").strip()[:80]
    if q:
        # Name, major, university and verified-skill names. Free-text `bio` is not searched: a LIKE over every
        # candidate's long text was the costliest part of the scan, and skills/major already describe the candidate.
        students = students.filter(
            Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(major__icontains=q)
            | Q(university__icontains=q)
            | Q(pk__in=qualifying_skills().filter(skill__name__icontains=q).values("student_id"))
        )
    if params.get("university"):
        students = students.filter(university=params["university"])  # exact: indexable (values come from the dropdown)
    if params.get("major"):
        students = students.filter(major=params["major"])

    shown = qualifying_skills().select_related("skill").order_by("-score", "skill__name")
    gigs = GigExperience.objects.verified().filter(student=OuterRef("pk")).values("student")  # subqueries: no join fan-out
    return (
        students.select_related("user")
        .annotate(
            best_score=Max("skills__score", filter=Q(skills__in=qualifying_skills())),
            gig_count=Coalesce(Subquery(gigs.annotate(n=Count("id")).values("n")), 0),
            gig_rating=Subquery(gigs.annotate(a=Avg("star_rating")).values("a")),
        )
        # Top Verified Candidates (≥ TOP_VERIFIED_MIN_GIGS completed gigs) come first.
        .annotate(top_verified=Case(When(gig_count__gte=TOP_VERIFIED_MIN_GIGS, then=Value(1)), default=Value(0), output_field=IntegerField()))
        .prefetch_related(Prefetch("skills", queryset=shown, to_attr="pool_skills"))
        .order_by("-top_verified", F("best_score").desc(nulls_last=True), "-pk")
    )


POOL_CACHE_GROUP = "talent_pool"  # bumped by internest_startups/signals.py when a candidate, skill or account changes
FILTER_OPTIONS_TTL = 300


def filter_options():
    """The search dropdowns (pool-wide distinct values): cached, shared by every company, invalidated on change."""
    from internest_core.page_cache import cached_value

    return cached_value(POOL_CACHE_GROUP, "filter_options", FILTER_OPTIONS_TTL, _compute_filter_options)


def _compute_filter_options():
    pool = pool_candidates()

    def distinct(field):  # stored values as-is, so the exact-match filter finds them
        return sorted({v for v in pool.exclude(**{f"{field}__isnull": True}).values_list(field, flat=True) if v and v.strip()},
                      key=str.lower)

    return {
        "skills": list(Skill.objects.filter(student_skills__in=qualifying_skills(), student_skills__student__in=pool)
                       .distinct().order_by("name")),
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
