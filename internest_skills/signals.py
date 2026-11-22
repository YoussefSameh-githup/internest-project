"""Cache invalidation for the skills app (connected in InternestSkillsConfig.ready)."""
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from internest_core.models import Internship
from internest_core.page_cache import bump

from .market_fit import CACHE_GROUP
from .models import Skill


@receiver(post_save, sender=Internship, dispatch_uid="skills_market_fit_internship_saved")
@receiver(post_delete, sender=Internship, dispatch_uid="skills_market_fit_internship_deleted")
@receiver(post_save, sender=Skill, dispatch_uid="skills_market_fit_skill_saved")
@receiver(post_delete, sender=Skill, dispatch_uid="skills_market_fit_skill_deleted")
def _opportunities_changed(sender, **kwargs):
    bump(CACHE_GROUP)


@receiver(m2m_changed, sender=Skill.opportunities.through, dispatch_uid="skills_market_fit_required_skills")
def _required_skills_changed(sender, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        bump(CACHE_GROUP)
