#!/usr/bin/env bash
# Daily backup for PythonAnywhere (Tasks tab → daily, e.g. 02:00):
#     bash /home/<user>/internest-project/scripts/backup.sh
# Optional environment: VENV (virtualenv path), BACKUP_KEEP (bundles to keep, default 14), BACKUP_DIR (see settings).
# Writes a bundle with the database and /media/, reads it back to verify it, prunes old bundles and appends to
# ~/backups/internest/backup.log. Exits non-zero on any failure, so the Tasks log shows it.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/.virtualenvs/internest312}"
KEEP="${BACKUP_KEEP:-14}"
LOG_DIR="${BACKUP_DIR:-$HOME/backups/internest}"

mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
umask 077

if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
elif [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "No virtualenv at $VENV. Run as: VENV=/home/<user>/.virtualenvs/<name> bash $0" >&2
    exit 1
fi
cd "$PROJECT_DIR"

{
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) backup start"
    python manage.py backup_data --keep "$KEEP"
    python manage.py db_health
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) backup done"
} >> "$LOG_DIR/backup.log" 2>&1 || {
    echo "Backup FAILED, see $LOG_DIR/backup.log" >&2
    tail -n 20 "$LOG_DIR/backup.log" >&2
    exit 1
}
tail -n 3 "$LOG_DIR/backup.log"
