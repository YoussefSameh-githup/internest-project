"""Send a test error to Sentry and say whether Sentry accepted it: python manage.py sentry_test"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from internest_core.observability import send_check


class Command(BaseCommand):
    help = "Send a test error to Sentry and report whether it was delivered (HTTP 200 = stored)."

    def handle(self, *args, **options):
        if not settings.SENTRY_DSN:
            raise CommandError("Sentry is not active: set SENTRY_DSN in .env and reload.")
        result = send_check("manage.py sentry_test")
        if not result["active"]:
            raise CommandError("Sentry is not active: set SENTRY_DSN in .env and reload.")
        line = (f"Event {result['event_id']} | environment '{settings.SENTRY_ENVIRONMENT}' | "
                f"transport: {result['transport']} | delivery: {result['delivery'] or 'queued'}")
        if result["delivery"] == 200:
            self.stdout.write(self.style.SUCCESS("Delivered. " + line))
        elif result["delivery"] is None:  # background thread: sent after flush, status not tracked yet
            self.stdout.write(self.style.WARNING("Sent (not confirmed). " + line))
        else:
            raise CommandError("Sentry did not accept the event. " + line)
