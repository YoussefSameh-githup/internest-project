from django.apps import AppConfig


class InternestCareersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "internest_careers"
    verbose_name = "Careers & interviews"

    def ready(self):
        from . import ai_tasks, signals  # noqa: F401  (engine task policies + mock answers, cache invalidation)
