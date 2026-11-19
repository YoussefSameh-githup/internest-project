"""Restore a backup bundle. First stop everything that uses the database (PythonAnywhere: Web tab → Disable, and
stop the always-on task run_async_tasks and any scheduled task), then:

    python manage.py restore_backup /path/internest-20261119-020000.tar.gz --yes
    python manage.py restore_backup /path/bundle.tar.gz --yes --no-media     # database only

The bundle is fully verified before anything changes; the current database is kept in BACKUP_DIR as
before-restore-<time>.sqlite3. A database still open by another process is refused.
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
        parser.add_argument("--force-side-files", action="store_true",
                            help="After a crash with nothing running: move leftover -wal/-journal files aside and go on.")
        parser.add_argument("--ignore-migrations", action="store_true",
                            help="Restore although the bundle lists migrations this code lacks (squashed or removed).")

    def handle(self, *args, **opts):
        if not opts["yes"]:
            raise CommandError("This replaces the live database. Stop the web app, then run again with --yes.")
        try:
            result = backups.restore_backup(opts["bundle"], include_media=not opts["no_media"],
                                            force_side_files=opts["force_side_files"],
                                            ignore_migrations=opts["ignore_migrations"])
        except backups.BackupError as exc:
            raise CommandError(f"Nothing was changed: {exc}") from exc
        for error in result["media_errors"]:
            self.stdout.write(self.style.WARNING(f"Media file not restored: {error}"))
        self.stdout.write(self.style.SUCCESS(
            f"Restored the database from {opts['bundle']} (taken {result['manifest']['created_at']} UTC) and "
            f"{result['media_files']} media file(s). Previous database kept at {result['safety_copy']}. "
            "Run `python manage.py migrate`, then enable and reload the web app."))
