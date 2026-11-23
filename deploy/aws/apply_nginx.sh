#!/usr/bin/env bash
# Installs the versioned nginx config for the EC2 server, with a backup and an automatic rollback if `nginx -t` fails.
#   sudo bash /home/ubuntu/internest/deploy/aws/apply_nginx.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SITE=/etc/nginx/sites-available/internest
HARDEN=/etc/nginx/conf.d/internest-hardening.conf
BACKUP=/root/nginx-backup-$(date -u +%Y%m%dT%H%M%SZ)

mkdir -p "$BACKUP"
cp -a "$SITE" "$BACKUP/" 2>/dev/null || true
cp -a "$HARDEN" "$BACKUP/" 2>/dev/null || true

install -m 644 "$HERE/nginx/internest.conf" "$SITE"
install -m 644 "$HERE/nginx/internest-hardening.conf" "$HARDEN"
ln -sf "$SITE" /etc/nginx/sites-enabled/internest

if nginx -t; then
  systemctl reload nginx
  echo "nginx config applied (backup in $BACKUP)."
else
  echo "nginx -t failed; restoring $BACKUP." >&2
  cp -a "$BACKUP/internest" "$SITE" 2>/dev/null || true
  if [ -f "$BACKUP/internest-hardening.conf" ]; then cp -a "$BACKUP/internest-hardening.conf" "$HARDEN"; else rm -f "$HARDEN"; fi
  nginx -t && systemctl reload nginx
  exit 1
fi
