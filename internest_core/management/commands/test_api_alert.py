"""Send sample admin alerts: python manage.py test_api_alert [--type failure|low_balance|both]

Goes to ADMIN_ALERT_EMAIL only, ignores the cooldown, and tells you whether SMTP accepted the mail."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from internest_core.api_monitor import AUTH, BALANCE, send_alert


class Command(BaseCommand):
    help = "Email a sample API failure and/or low-balance alert to ADMIN_ALERT_EMAIL."

    def add_arguments(self, parser):
        parser.add_argument("--type", choices=("failure", "low_balance", "both"), default="both")

    def handle(self, *args, **options):
        if not settings.ADMIN_ALERT_EMAIL:
            raise CommandError("ADMIN_ALERT_EMAIL is not set.")
        samples = []
        if options["type"] in ("failure", "both"):
            samples.append(("API failure", AUTH, 401, "TEST ALERT: sample 401 from the test_api_alert command. Nothing is wrong."))
        if options["type"] in ("low_balance", "both"):
            samples.append(("Low balance", BALANCE, None, "TEST ALERT: remaining balance 1.50 is at or below the threshold "
                                                          f"{settings.API_LOW_BALANCE_THRESHOLD:g}. Nothing is wrong."))
        failed = 0
        for label, alert_type, status, detail in samples:
            sent = send_alert("Test Provider", "test_api_alert command", alert_type, status, detail, force=True)
            failed += not sent
            self.stdout.write(f"{label}: {'sent' if sent else 'NOT sent (check EMAIL_HOST_PASSWORD / SMTP settings)'}")
        if failed:
            raise CommandError(f"{failed} alert(s) not sent.")
        self.stdout.write(self.style.SUCCESS(f"Sent to {settings.ADMIN_ALERT_EMAIL}"))
