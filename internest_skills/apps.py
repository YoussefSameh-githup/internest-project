from django.apps import AppConfig


class InternestSkillsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "internest_skills"
    verbose_name = "Skill Intelligence"

    def ready(self):
        from . import ai_tasks, signals  # noqa: F401  (signals: market-fit cache invalidation)

        ai_tasks.register_all()
