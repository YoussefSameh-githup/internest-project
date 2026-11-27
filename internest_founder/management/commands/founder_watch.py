"""Founder alerts (worker, queue, disk, backups, API errors), the daily summary and retention. Every ~10 minutes."""
from django.core.management.base import BaseCommand

from internest_founder import watch
from internest_founder.models import FounderSettings


class Command(BaseCommand):
    help = "Raise deduplicated founder alerts, send the daily summary when due, purge old founder records."

    def handle(self, *args, **options):
        prefs = FounderSettings.load()
        raised = watch.check_alerts(prefs)
        sent = watch.daily_summary(prefs)
        purged = watch.purge()
        self.stdout.write(f"alerts={','.join(raised) or 'none'} summary={'sent' if sent else 'no'} purged={purged}")
