"""Invalidate the cached Talent Pool aggregates (talent.filter_options) when what they are built from changes."""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from internest_core.page_cache import bump

from .talent import POOL_CACHE_GROUP


@receiver([post_save, post_delete], sender="internest_core.StudentProfile")
@receiver([post_save, post_delete], sender="internest_skills.StudentSkill")
@receiver([post_save, post_delete], sender="internest_skills.Skill")
@receiver([post_save, post_delete], sender=get_user_model())
def _talent_pool_changed(sender, update_fields=None, **kwargs):
    if update_fields and set(update_fields) <= {"last_login"}:
        return  # every sign-in saves last_login: not a pool change
    bump(POOL_CACHE_GROUP)
