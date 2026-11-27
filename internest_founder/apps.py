from django.apps import AppConfig


class InternestFounderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "internest_founder"
    verbose_name = "Founder Control Center"

    def ready(self):
        from . import signals  # noqa: F401  (business events → notifications + n8n outbox)
        from . import jobs  # noqa: F401  (webhook delivery job on the existing async queue)
