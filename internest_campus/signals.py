"""Invalidate the cached campus leaderboard whenever a vote is cast or removed."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from internest_core.page_cache import bump

from .models import CampusDemandVote


@receiver(post_save, sender=CampusDemandVote)
@receiver(post_delete, sender=CampusDemandVote)
def _campus_changed(sender, **kwargs):
    bump("campus")
