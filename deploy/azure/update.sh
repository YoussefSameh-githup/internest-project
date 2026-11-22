#!/usr/bin/env bash
# Deploy the latest code from GitHub (the server's equivalent of "git pull + Reload" on PythonAnywhere):
#
#   sudo bash /srv/internest/app/deploy/azure/update.sh
#
# Pulls main, installs requirements, migrates, collects static files, restarts gunicorn and the async task worker and
# checks /healthz/ answers.
# (Translations are compiled before commit: the .mo files live in git, so compilemessages here would dirty the
# checkout and break the next pull.) Stops at the first failing step; the site keeps running the previous code until the restart.
set -euo pipefail

BASE=/srv/internest
APP=$BASE/app
VENV=$BASE/venv
PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo." >&2; exit 1; }
[ -f "$APP/.env" ] || { echo "$APP/.env is missing: upload it first (see deploy/azure/README.md)." >&2; exit 1; }
as_app() { sudo -u internest -H "$@"; }
cd "$APP"

[ "$PULL" -eq 0 ] || as_app git pull --ff-only --quiet
as_app "$VENV/bin/pip" install --quiet -r deploy/azure/requirements-server.txt
as_app "$VENV/bin/python" manage.py migrate --noinput
as_app "$VENV/bin/python" manage.py collectstatic --noinput --verbosity 0
systemctl restart internest
# The async task worker runs the same code: restart it too (installed by setup.sh; skipped on older servers).
if [ -f /etc/systemd/system/internest-worker.service ]; then
    systemctl restart internest-worker
fi

HOST="$(grep -E '^DJANGO_ALLOWED_HOSTS=' .env | head -1 | cut -d= -f2 | cut -d, -f1)"
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 --unix-socket /run/internest/gunicorn.sock -H "Host: $HOST" \
            -H "X-Forwarded-Proto: https" "http://$HOST/healthz/" >/dev/null 2>&1; then
        echo "Deployed $(as_app git rev-parse --short HEAD): the app answers on /healthz/."
        exit 0
    fi
    sleep 1
done
echo "The app did not answer /healthz/ within 30 s. Logs: sudo journalctl -u internest -n 80" >&2
exit 1
