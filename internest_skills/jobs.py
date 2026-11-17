"""Async jobs of the skills app (registered at startup by internest_core.async_tasks.autodiscover)."""
from internest_core.async_tasks import register


@register("skill_challenge_start")
def skill_challenge_start_job(user_id, student_skill_id):
    from internest_core.models import StudentProfile

    from .views import challenge_outcome

    return challenge_outcome(StudentProfile.objects.get(user_id=user_id), student_skill_id)
