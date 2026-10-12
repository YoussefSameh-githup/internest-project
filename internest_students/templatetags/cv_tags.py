from django import template
from django.db import DatabaseError, transaction

from ..models import StudentCV

register = template.Library()


@register.inclusion_tag("students/_cv_card.html")
def cv_builder_card(profile):
    """Profile sidebar: CV builder entry point and the projects synced from it."""
    if profile is None or not profile.pk:
        return {"profile": None}
    try:
        with transaction.atomic():  # savepoint, so a missing table can't break the page's transaction
            projects = list(profile.projects.all()[:5])
            has_cv = StudentCV.objects.filter(student=profile).exclude(data={}).exists()
    except DatabaseError:  # tables not migrated yet: the profile page must still render
        projects, has_cv = [], False
    return {"profile": profile, "projects": projects, "has_cv": has_cv}
