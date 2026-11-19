"""Database health: integrity, size, biggest tables and the plan of every hot query (full table scans flagged).

    python manage.py db_health              # summary
    python manage.py db_health --plans      # plus each query plan
    python manage.py db_health --optimize   # also run SQLite's PRAGMA optimize (refreshes planner statistics)
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from internest_core import db_health


class Command(BaseCommand):
    help = "Report database integrity, size and hot-query plans."

    def add_arguments(self, parser):
        parser.add_argument("--plans", action="store_true")
        parser.add_argument("--optimize", action="store_true")

    def handle(self, *args, **opts):
        if opts["optimize"]:
            with connection.cursor() as cur:
                cur.execute("PRAGMA optimize")
        report = db_health.database_report()
        style = self.style.SUCCESS if report["quick_check"] == "ok" else self.style.ERROR
        self.stdout.write(style(f"Integrity: {report['quick_check']}"))
        self.stdout.write(f"Size: {report['size_mb']} MB ({report['free_pct']}% free pages), journal: "
                          f"{report['journal_mode']}, {report['tables']} tables")
        self.stdout.write("Biggest tables: " + ", ".join(f"{t} {n}" for t, n in report["biggest_tables"]))
        problems = 0
        for row in db_health.query_plans():
            if row["full_scans"]:
                problems += 1
                self.stdout.write(self.style.WARNING(f"FULL SCAN  {row['name']}: {', '.join(row['full_scans'])}"))
            else:
                self.stdout.write(f"indexed    {row['name']}")
            if opts["plans"]:
                self.stdout.write("    " + row["plan"].replace("\n", "\n    "))
        self.stdout.write(self.style.SUCCESS("All hot queries use an index.") if not problems
                          else self.style.WARNING(f"{problems} hot query(ies) scan a whole table."))
        if report["quick_check"] != "ok":  # a failing exit code makes the backup task's log show it
            raise CommandError(f"Database integrity check failed: {report['quick_check']}")
