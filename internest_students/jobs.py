"""Async jobs of the CV builder (registered at startup by internest_core.async_tasks.autodiscover)."""
from internest_core.async_tasks import register


@register("cv_polish")
def cv_polish_job(user_id, cv):
    from .views import polish_outcome

    return polish_outcome(cv)


@register("cv_voice")
def cv_voice_job(user_id, cv, audio, filename, content_type):
    from .views import voice_outcome

    return voice_outcome(cv, audio, filename, content_type)
