#!/usr/bin/env bash
# Daily backup for PythonAnywhere (Tasks tab → daily, e.g. 02:00 UTC):
#     VENV=/home/<user>/.virtualenvs/<venv-name> bash /home/<user>/internest-project/scripts/backup.sh
# Optional environment: BACKUP_KEEP (full bundles to keep, default 14), BACKUP_MEDIA=0 (database only),
# BACKUP_LOG_DIR (default ~/backups/internest; keep it equal to BACKUP_DIR in .env).
# Each step is checked on its own: the first failure is logged and the script exits non-zero, so the Tasks page
# shows the error. (A `{ ...; } || ...` group would switch `set -e` off inside it and hide failures.)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$HOME/.virtualenvs/internest312}"
KEEP="${BACKUP_KEEP:-14}"
LOG_DIR="${BACKUP_LOG_DIR:-$HOME/backups/internest}"
LOG="$LOG_DIR/backup.log"

umask 077
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"

log() { echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }

fail() {
    log "FAILED: $1"
    echo "Backup FAILED at: $1 (see $LOG)" >&2
    tail -n 20 "$LOG" >&2
    exit 1
}

step() {  # step <label> <command...>: run it, append its output to the log, stop on failure
    local label="$1"
    shift
    "$@" >> "$LOG" 2>&1 || fail "$label"
}

if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
elif [ -z "${VIRTUAL_ENV:-}" ]; then
    echo "No virtualenv at $VENV. Run as: VENV=/home/<user>/.virtualenvs/<name> bash $0" >&2
    exit 1
fi
cd "$PROJECT_DIR"

log "backup start"
if [ "${BACKUP_MEDIA:-1}" = "0" ]; then
    step "backup_data --no-media" python manage.py backup_data --no-media --keep "$KEEP"
else
    step "backup_data" python manage.py backup_data --keep "$KEEP"
fi
step "db_health" python manage.py db_health
log "backup done"
tail -n 3 "$LOG"
