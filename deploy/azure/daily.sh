#!/usr/bin/env bash
# Daily jobs (run by internest-daily.timer): the PythonAnywhere "Tasks" list, in one place.
# Each job runs even if an earlier one failed; the unit fails (visible in `systemctl status internest-daily`) if any did.
set -uo pipefail

APP=/srv/internest/app
VENV=/srv/internest/venv
status=0
cd "$APP"

VENV="$VENV" BACKUP_LOG_DIR=/srv/internest/backups/internest bash scripts/backup.sh || status=1
"$VENV/bin/python" manage.py purge_security_events || status=1
"$VENV/bin/python" manage.py check_api_health || status=1
exit $status
