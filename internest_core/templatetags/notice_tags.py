from django import template

from internest_core import identity, notices

register = template.Library()


@register.simple_tag
def student_notices(user):
    """Template paths of the in-app notices this student still has to see (no query: the profile is memoised)."""
    from internest_skills.permissions import is_student_role

    if not is_student_role(user):
        return []
    profile = identity.student_profile(user)
    return [f"components/notices/{key}.html" for key in notices.pending(profile)] if profile else []
