#!/usr/bin/env bash
# Weekly refresh of Cloudflare's edge ranges for nginx (real client IPs), every Sunday 03:00 (the server clock is UTC).
#   sudo bash /home/ubuntu/internest/deploy/aws/install_cloudflare_cron.sh
# The job runs a root-owned copy in /usr/local/sbin, not the file in the app checkout: a root cron job must not run
# a script that the app user (or a git pull) can change. Re-run this installer after the script changes in git.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
BIN=/usr/local/sbin/update-cloudflare-ips
CRON=/etc/cron.d/update-cloudflare-ips
LOG=/var/log/update-cloudflare-ips.log

install -o root -g root -m 755 "$HERE/update_cloudflare_ips.sh" "$BIN"
touch "$LOG" && chown root:adm "$LOG" && chmod 640 "$LOG"

cat > "$CRON.tmp" <<EOF
# Installed by deploy/aws/install_cloudflare_cron.sh. Refreshes /etc/nginx/conf.d/cloudflare-realip.conf.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 3 * * 0 root $BIN >> $LOG 2>&1
EOF
chown root:root "$CRON.tmp" && chmod 644 "$CRON.tmp" && mv "$CRON.tmp" "$CRON"  # cron.d files must be root-owned, 644

echo "Installed $BIN and $CRON (Sundays 03:00, log: $LOG)."
