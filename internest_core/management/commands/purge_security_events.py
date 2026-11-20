"""Delete audit events older than SECURITY_EVENT_RETENTION_DAYS (default 365). Schedule daily on PythonAnywhere."""
from django.core.management.base import BaseCommand

from internest_core.audit import purge_old_events, retention_days


class Command(BaseCommand):
    help = "Delete security audit events older than the retention period."

    def handle(self, *args, **options):
        from internest_core.password_reset import purge_stale

        deleted = purge_old_events()
        limits = purge_stale()
        self.stdout.write(f"Deleted {deleted} security event(s) older than {retention_days()} days "
                          f"and {limits} idle password-reset limit row(s).")
