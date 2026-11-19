"""Back up the database and uploaded files into one checked bundle (see internest_core/backups.py, docs in BACKUP.md).

    python manage.py backup_data                # database + media, keep the newest 14 bundles
    python manage.py backup_data --no-media     # database only (fast; use between full backups)
    python manage.py backup_data --keep 30 --dir /home/<user>/backups/internest
"""
from django.core.management.base import BaseCommand, CommandError

from internest_core import backups


class Command(BaseCommand):
    help = "Create a verified backup bundle of the SQLite database and MEDIA_ROOT, then prune old bundles."

    def add_arguments(self, parser):
        parser.add_argument("--no-media", action="store_true", help="Back up the database only.")
        parser.add_argument("--keep", type=int, default=14, help="How many bundles to keep (default 14).")
        parser.add_argument("--dir", default="", help="Where to write bundles (default: BACKUP_DIR or ~/backups/internest).")

    def handle(self, *args, **opts):
        try:
            bundle = backups.create_backup(include_media=not opts["no_media"], dest_dir=opts["dir"] or None)
            manifest = backups.verify_backup(bundle)  # a backup that cannot be read back is not a backup
            pruned = backups.prune_backups(opts["keep"], dest_dir=opts["dir"] or None)
        except backups.BackupError as exc:
            raise CommandError(str(exc)) from exc
        files = len(manifest["files"]) - 1
        size_mb = bundle.stat().st_size / 1024 / 1024
        self.stdout.write(self.style.SUCCESS(
            f"Backup OK: {bundle} ({size_mb:.1f} MB, database + {files} media file(s)); {len(pruned)} old bundle(s) removed."))
