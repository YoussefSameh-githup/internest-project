"""Validate every registered API key and check the paid balances: python manage.py check_api_health

Schedule it daily on PythonAnywhere (Tasks tab). Real problems email ADMIN_ALERT_EMAIL (30-minute cooldown); no fake test alerts
are sent. Exit status 1 when a key is rejected/unreachable or a balance is low, so the task log shows it too."""
from django.core.management.base import BaseCommand, CommandError

from internest_core.api_health import run_health_check


class Command(BaseCommand):
    help = "Validate the keys of all registered external APIs and check paid balances (alerts only on real problems)."

    def handle(self, *args, **options):
        results = run_health_check()
        width = max(len(r.name) for r in results)
        self.stdout.write(f"{'API'.ljust(width)}  {'KEY':<5} {'DETAIL':<52} BALANCE")
        for r in results:
            self.stdout.write(f"{r.name.ljust(width)}  {r.key_status:<5} {r.key_note:<52} {r.balance_status}: {r.balance_note}")
        problems = [r for r in results if r.problem]
        if problems:
            raise CommandError(f"{len(problems)} API problem(s): " + ", ".join(r.name for r in problems))
        self.stdout.write(self.style.SUCCESS("All configured APIs look healthy."))
