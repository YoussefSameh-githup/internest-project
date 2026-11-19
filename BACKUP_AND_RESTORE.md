# Backup and restore

Internest keeps everything in two places: the SQLite database (`db.sqlite3`) and uploaded files (`media/`: CVs, photos, logos, certificates, freelance proof, payment receipts, experience letters).

## What a backup is
Bundles live in `BACKUP_DIR`. That's an absolute path from `.env`; the default is `~/backups/internest`.

| bundle | content |
|---|---|
| `internest-full-<UTC time>.tar.gz` | database + every media file |
| `internest-db-<UTC time>.tar.gz` | database only (`--no-media`) |

Each bundle holds:
- `db.sqlite3`: a consistent copy taken with SQLite's online backup API, so the site can keep running. Its journal mode is reset to DELETE, which is safe on PythonAnywhere's disk.
- `media/…`: the uploaded files.
- `manifest.json`, written last: every file's SHA-256 and size, plus the applied migrations.

Full and database-only bundles are pruned separately. Each kind keeps its newest 14 by default (`--keep`).

**Privacy.** Bundles contain private data: CVs, receipts, emails and password hashes.
- Files are created owner-only (`0600`), and the folder is `0700`.
- The command refuses a relative path, the home folder itself, the project, `MEDIA_ROOT` and `STATIC_ROOT`.
- `.gitignore` also covers `*.tar.gz`, `db.sqlite3.*` and `*.sqlite3-*`.
- **Never upload a bundle to a public place or a shared drive.** Keep an off-site copy on an encrypted disk you control.

## Daily backup (PythonAnywhere)
Tasks tab → add a daily task (e.g. 02:00 UTC):
```bash
VENV=/home/<user>/.virtualenvs/<venv-name> bash /home/<user>/internest-project/scripts/backup.sh
```
The script:
- creates a full bundle and reads it back to verify it (checksums, archive contents, `integrity_check`);
- prunes old full bundles;
- runs `db_health` (`quick_check` + query plans);
- appends everything to `~/backups/internest/backup.log`.

Each step is checked on its own. The first failure is logged as `FAILED: <step>`, and the task exits with an error that the Tasks page shows.

Optional variables:
- `BACKUP_KEEP=30`: how many bundles to keep.
- `BACKUP_MEDIA=0`: database only. Use this for an extra, more frequent task; it never removes the full bundles.
- `BACKUP_LOG_DIR`: where the log goes. Keep it the same as `BACKUP_DIR` in `.env`.

Manual commands:
```bash
python manage.py backup_data                 # full bundle
python manage.py backup_data --no-media      # database only
python manage.py verify_backup               # check the newest bundle
python manage.py verify_backup <bundle>      # check a specific one
python manage.py db_health --plans           # quick_check, size, hot-query plans (exit code 1 if quick_check fails)
```

**Disk space:** peak use is (keep + 1) full bundles plus one temporary database copy. Check with `du -sh ~/backups/internest`. If the account quota gets tight, lower `BACKUP_KEEP`: a full quota stops SQLite from writing, which takes the site down.

## Off-site copy (weekly)
Files tab → `backups/internest/` → download the newest `internest-full-…` bundle to your own encrypted storage. You can check it on any machine with the project: `python manage.py verify_backup /path/to/bundle.tar.gz`.

## Restore
1. **Stop everything that uses the database:**
   - Web tab → **Disable** the web app.
   - Tasks tab → stop the always-on task (`run_async_tasks`) and pause scheduled tasks.

   The restore refuses when it can see another process using the database. The checks are: a lock, leftover `-wal`/`-journal` files, and on Linux any process of your user that has the file open. That check is best effort, though: a web worker on another PythonAnywhere server can't be seen, so stopping everything is still your job.
2. Optional dry check: `python manage.py verify_backup <bundle>`.
3. Restore:
   ```bash
   cd ~/internest-project
   python manage.py restore_backup <bundle> --yes             # database + media
   python manage.py restore_backup <bundle> --yes --no-media  # database only
   ```
   Everything is checked **before** the database is touched:
   - checksums, sizes, and duplicate, linked or unsafe archive members;
   - every media target path;
   - the database's `integrity_check`;
   - that the bundle isn't from newer code;
   - that no other process is visibly using the database.

   If any check fails, nothing changes. Two overrides, each for one case only:
   - `--force-side-files`: after a crash with nothing running. Leftover `-wal`/`-journal` files are moved next to the safety copy instead of blocking the restore.
   - `--ignore-migrations`: when the bundle lists migrations that were squashed or removed on purpose. After that:
   - The current database is saved as `BACKUP_DIR/before-restore-<time>.sqlite3` (or `.raw` if the live file was corrupt).
   - The new database is swapped in atomically.
   - Media files from the bundle are written back. Files not in the bundle are kept, and any file that couldn't be written is listed.
4. `python manage.py migrate` (only needed when the code is newer than the backup).
5. Start the tasks, enable the web app and Reload. Sign in and open a CV and a receipt to check.

**Undo a restore** (with everything stopped again):
1. Delete any `db.sqlite3-wal`, `db.sqlite3-shm` or `db.sqlite3-journal` next to the database.
2. Copy `before-restore-<time>.sqlite3` over `db.sqlite3`.

## Restore drill (monthly)
Restore the newest bundle into a scratch location; this doesn't touch the live site:
```bash
python manage.py shell -c "from internest_core import backups; r = backups.restore_backup((backups.list_backups(kind='full') or backups.list_backups())[-1], db_path='/tmp/drill.sqlite3', media_root='/tmp/drill_media', safety_dir='/tmp/drill_safety'); print(r['media_files'], 'files')"
sqlite3 /tmp/drill.sqlite3 "select count(*) from auth_user;"
rm -rf /tmp/drill.sqlite3* /tmp/drill_media /tmp/drill_safety
```

## Limits
- SQLite only. If the site moves to PostgreSQL, use the provider's backups (`backup_data` refuses other databases).
- Bundles are not encrypted. Their protection is file permissions on the PythonAnywhere account plus your own encrypted off-site storage.
- With daily backups, up to 24 h of data can be lost. Add a more frequent `BACKUP_MEDIA=0` task if that is too much.
- Files deleted by users while a backup runs are skipped and listed in the manifest (`skipped`).
