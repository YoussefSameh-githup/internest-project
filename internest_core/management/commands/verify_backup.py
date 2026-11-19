"""Check a backup bundle without restoring it (checksums, archive contents, SQLite integrity_check).

    python manage.py verify_backup              # the newest bundle in BACKUP_DIR
    python manage.py verify_backup /path/internest-20261119-020000.tar.gz
"""
from django.core.management.base import BaseCommand, CommandError

from internest_core import backups


class Command(BaseCommand):
    help = "Verify a backup bundle (default: the newest one)."

    def add_arguments(self, parser):
        parser.add_argument("bundle", nargs="?", default="")
        parser.add_argument("--dir", default="")

    def handle(self, *args, **opts):
        bundle = opts["bundle"]
        if not bundle:
            found = backups.list_backups(opts["dir"] or None)
            if not found:
                raise CommandError(f"No backups found in {opts['dir'] or backups.backup_dir()}")
            bundle = found[-1]
        try:
            manifest = backups.verify_backup(bundle)
        except backups.BackupError as exc:
            raise CommandError(f"Backup FAILED verification: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(
            f"Backup OK: {bundle} (taken {manifest['created_at']} UTC, {len(manifest['files'])} file(s), "
            f"database integrity ok)."))
