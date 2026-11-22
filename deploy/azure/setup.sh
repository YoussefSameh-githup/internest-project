#!/usr/bin/env bash
# One-time (and re-runnable) setup of Internest on a fresh Ubuntu 24.04 server: an Azure for Students VM or any VPS.
#
#   sudo bash setup.sh --domain internest.app --email you@example.com
#
# Run it once to install everything; it stops and tells you to upload your .env. Upload it, then run the SAME command
# again: it finishes (migrations, static files, HTTPS certificate) and checks the site answers.
#
# Options:
#   --domain NAME   the site's domain (its DNS A record must point at this server before HTTPS can be issued)
#   --email ADDR    for the free Let's Encrypt certificate (expiry notices only)
#   --no-tls        skip HTTPS (testing, or while DNS still points elsewhere; run again without it later)
#   --repo URL / --branch NAME   where to get the code (default: the GitHub repo, main)
#
# Layout: code /srv/internest/app, virtualenv /srv/internest/venv, backups /srv/internest/backups/internest,
# service "internest" (gunicorn on a unix socket) behind nginx, daily timer "internest-daily".
set -euo pipefail

DOMAIN=""
EMAIL=""
TLS=1
REPO="https://github.com/YoussefSameh-githup/internest-project.git"
BRANCH="main"
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="${2:-}"; shift 2 ;;
        --email) EMAIL="${2:-}"; shift 2 ;;
        --no-tls) TLS=0; shift ;;
        --repo) REPO="${2:-}"; shift 2 ;;
        --branch) BRANCH="${2:-}"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo." >&2; exit 1; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo "--domain is required (e.g. --domain internest.app)" >&2; exit 2; }
[ "$TLS" -eq 0 ] || [ -n "$EMAIL" ] || { echo "--email is required for the HTTPS certificate (or pass --no-tls)" >&2; exit 2; }

APP_USER=internest
BASE=/srv/internest
APP=$BASE/app
VENV=$BASE/venv
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEBIAN_FRONTEND=noninteractive
as_app() { sudo -u "$APP_USER" -H "$@"; }

echo "== 1/7 System packages and automatic security updates"
apt-get update -q
apt-get upgrade -yq
apt-get install -yq python3 python3-venv python3-dev build-essential git nginx gettext sqlite3 curl \
    ufw fail2ban unattended-upgrades certbot python3-certbot-nginx
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now fail2ban >/dev/null

echo "== 2/7 Swap (small VMs have 1 GB of RAM: pip installs and traffic peaks need headroom)"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    [ -f /swapfile ] || { fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null; }
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== 3/7 App user, code and virtualenv"
id "$APP_USER" >/dev/null 2>&1 || adduser --system --group --home "$BASE" --shell /bin/bash "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$BASE" "$BASE/backups"
cd "$BASE"  # commands run as the app user must not start in a folder it cannot read (e.g. /home/azureuser)
if [ ! -d "$APP/.git" ]; then
    as_app git clone --quiet --branch "$BRANCH" "$REPO" "$APP"
fi
[ -x "$VENV/bin/python" ] || as_app python3 -m venv "$VENV"
as_app "$VENV/bin/pip" install --quiet --upgrade pip
as_app "$VENV/bin/pip" install --quiet -r "$APP/deploy/azure/requirements-server.txt"

echo "== 4/7 gunicorn service and the daily job"
usermod -aG "$APP_USER" www-data  # nginx reaches the socket through the group
install -m 644 "$HERE/internest.service" /etc/systemd/system/internest.service
install -m 644 "$HERE/internest-daily.service" /etc/systemd/system/internest-daily.service
install -m 644 "$HERE/internest-daily.timer" /etc/systemd/system/internest-daily.timer
systemctl daemon-reload
systemctl enable internest.service >/dev/null
systemctl enable --now internest-daily.timer >/dev/null

echo "== 5/7 nginx"
sed "s/__DOMAIN__/$DOMAIN/g" "$HERE/nginx.conf" > /etc/nginx/sites-available/internest
ln -sf /etc/nginx/sites-available/internest /etc/nginx/sites-enabled/internest
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl reload nginx

echo "== 6/7 Firewall: SSH, HTTP and HTTPS only"
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

if [ ! -f "$APP/.env" ]; then
    cat <<EOF

Almost done. Upload your .env (the one from PythonAnywhere: it holds the secret key and API keys) to:
    $APP/.env
then run this same command again. The domain lines in it are updated for you.
EOF
    exit 0
fi

echo "== 7/7 Settings for this server, deploy, HTTPS"
chown "$APP_USER:$APP_USER" "$APP/.env"
chmod 600 "$APP/.env"
as_app "$VENV/bin/python" "$HERE/configure_env.py" "$APP/.env" --domain "$DOMAIN" --tls "$TLS" --backup-dir "$BASE/backups/internest"
bash "$HERE/update.sh" --no-pull
if [ "$TLS" -eq 1 ]; then
    # Re-runs rewrite the nginx file from the template; --keep-until-expiring re-installs the existing certificate.
    certbot --nginx --non-interactive --agree-tos --redirect --keep-until-expiring -m "$EMAIL" -d "$DOMAIN"
    systemctl reload nginx
    curl -fsS --max-time 20 "https://$DOMAIN/healthz/" >/dev/null && echo "https://$DOMAIN is live."
else
    echo "Running over plain HTTP (--no-tls). Run again without --no-tls once DNS points here."
fi
