"""Career recommendation cache invalidation (see career_service: groups "careers" and "careers:<student id>")."""
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from internest_core.models import Internship
from internest_core.page_cache import bump
from internest_skills.models import Course, Skill, StudentSkill


@receiver([post_save, post_delete], sender=StudentSkill, dispatch_uid="careers_student_skill")
def _student_skill_changed(sender, instance, **kwargs):
    bump(f"careers:{instance.student_id}")


@receiver([post_save, post_delete], sender=Internship, dispatch_uid="careers_internship")
@receiver([post_save, post_delete], sender=Course, dispatch_uid="careers_course")
def _market_changed(sender, **kwargs):
    bump("careers")  # new role paths token: every student's cached result is recomputed on the next visit


@receiver(m2m_changed, sender=Skill.opportunities.through, dispatch_uid="careers_required_skills")
def _required_skills_changed(sender, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        bump("careers")
