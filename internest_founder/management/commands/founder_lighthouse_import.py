"""Import a Lighthouse JSON report (lab data) into the Performance Center.

    lighthouse https://internest.app/ --output=json --output-path=home.json   (any machine with Chrome)
    manage.py founder_lighthouse_import home.json
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from internest_founder import perf


class Command(BaseCommand):
    help = "Import Lighthouse JSON reports."

    def add_arguments(self, parser):
        parser.add_argument("reports", nargs="+")

    def handle(self, *args, reports, **options):
        for name in reports:
            raw = Path(name).read_bytes()
            if not perf.report_json_size_ok(raw):
                raise CommandError(f"{name}: larger than 8 MB")
            try:
                run = perf.import_lighthouse(perf.loads_report(raw))
            except ValueError as exc:
                raise CommandError(f"{name}: {exc}")
            score = (run.data.get("scores") or {}).get("performance")
            self.stdout.write(f"{name}: {run.url} performance={round(score * 100) if score is not None else '-'} "
                              f"LCP={run.data['metrics'].get('LCP')} CLS={run.data['metrics'].get('CLS')}")
