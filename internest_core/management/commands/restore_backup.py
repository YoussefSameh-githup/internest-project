"""Restore a backup bundle. Stop the web app first (PythonAnywhere: Web tab → Disable), then:

    python manage.py restore_backup /path/internest-20261119-020000.tar.gz --yes
    python manage.py restore_backup /path/bundle.tar.gz --yes --no-media     # database only

The bundle is fully verified before anything changes; the current database is kept as db.sqlite3.before-restore-<time>.
Media files in the bundle are written back; files that are not in the bundle are left alone.
"""
from django.core.management.base import BaseCommand, CommandError

from internest_core import backups


class Command(BaseCommand):
    help = "Replace the database (and restore media files) from a verified backup bundle."

    def add_arguments(self, parser):
        parser.add_argument("bundle")
        parser.add_argument("--yes", action="store_true", help="Confirm: the current database will be replaced.")
        parser.add_argument("--no-media", action="store_true", help="Restore the database only.")

    def handle(self, *args, **opts):
        if not opts["yes"]:
            raise CommandError("This replaces the live database. Stop the web app, then run again with --yes.")
        try:
            result = backups.restore_backup(opts["bundle"], include_media=not opts["no_media"])
        except backups.BackupError as exc:
            raise CommandError(f"Nothing was changed: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Restored the database from {opts['bundle']} (taken {result['manifest']['created_at']} UTC) and "
            f"{result['media_files']} media file(s). Previous database kept at {result['safety_copy']}. "
            "Run `python manage.py migrate`, then enable and reload the web app."))
