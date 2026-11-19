"""Delete audit events older than SECURITY_EVENT_RETENTION_DAYS (default 365). Schedule daily on PythonAnywhere."""
from django.core.management.base import BaseCommand

from internest_core.audit import purge_old_events, retention_days


class Command(BaseCommand):
    help = "Delete security audit events older than the retention period."

    def handle(self, *args, **options):
        deleted = purge_old_events()
        self.stdout.write(f"Deleted {deleted} security event(s) older than {retention_days()} days.")
