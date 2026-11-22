from django.apps import AppConfig


class InternestStudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "internest_students"
    verbose_name = "Student CV Builder"

    def ready(self):
        from . import ai_tasks  # noqa: F401  (cv_polish engine policy + mock answer)
