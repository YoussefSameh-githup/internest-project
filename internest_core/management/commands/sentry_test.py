"""Send a test event to Sentry: python manage.py sentry_test"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test message to Sentry to confirm the DSN and network access work."

    def handle(self, *args, **options):
        import sentry_sdk

        if not settings.SENTRY_DSN or not sentry_sdk.get_client().is_active():
            raise CommandError("Sentry is not active: set SENTRY_DSN in .env and reload.")
        event_id = sentry_sdk.capture_message("Internest Sentry test event (manage.py sentry_test)", level="info")
        sentry_sdk.flush(timeout=10)
        self.stdout.write(self.style.SUCCESS(f"Sent test event {event_id} to environment '{settings.SENTRY_ENVIRONMENT}'."))
