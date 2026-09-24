import logging

from django import template
from django.db import DatabaseError

from ..models import StudentSkill
from ..permissions import is_pro_employer, is_verified_university, is_student_role, skill_view_role

register = template.Library()
logger = logging.getLogger(__name__)


@register.inclusion_tag("skills/_profile_card.html", takes_context=True)
def skills_profile_card(context, student):
    skills = []
    if student is not None and student.pk:
        try:
            skills = list(student.skills.select_related("skill"))
        except DatabaseError:
            # Profile page must still render if skills tables are unavailable (e.g. migration not applied yet).
            logger.exception("Could not load skills for profile card")
    return {
        "request": context.get("request"),
        "verified": [s for s in skills if s.status == StudentSkill.STATUS_VERIFIED],
        "lag_count": sum(1 for s in skills if s.status == StudentSkill.STATUS_LAG),
        "claimed_count": sum(1 for s in skills if s.status == StudentSkill.STATUS_CLAIMED),
    }


@register.simple_tag
def can_view_skills(user, student):
    return skill_view_role(user, student) is not None


@register.simple_tag
def partner_is_pro(partner):
    return is_pro_employer(partner)


@register.simple_tag
def partner_is_verified_university(partner):
    return is_verified_university(partner)


@register.simple_tag
def is_student_user(user):
    return is_student_role(user)
