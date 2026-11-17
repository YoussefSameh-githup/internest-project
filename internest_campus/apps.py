from django.apps import AppConfig


class InternestCampusConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "internest_campus"
    verbose_name = "Campus demand campaign"

    def ready(self):
        from . import signals  # noqa: F401  (leaderboard cache invalidation)
