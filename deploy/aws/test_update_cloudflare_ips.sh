#!/usr/bin/env bash
# Failure-path tests for update_cloudflare_ips.sh. Needs no root and touches nothing in /etc: it runs a copy of the
# script against fixture lists (file:// URLs), a temp conf dir and stub nginx / systemctl that log their calls.
# Run on any Linux box: bash deploy/aws/test_update_cloudflare_ips.sh
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/conf" "$W/fx"
cat > "$W/bin/nginx" <<EOF
#!/bin/sh
echo "nginx \$*" >> "$W/calls"; [ ! -f "$W/nginx_fails" ]
EOF
cat > "$W/bin/systemctl" <<EOF
#!/bin/sh
echo "systemctl \$*" >> "$W/calls"; [ ! -f "$W/reload_fails" ]
EOF
chmod +x "$W/bin/"*
# same script, with the stub dir first on its fixed PATH
sed "s#^export PATH=#export PATH=$W/bin:#" "$HERE/update_cloudflare_ips.sh" > "$W/run.sh"

for i in $(seq 1 15); do echo "104.$i.0.0/16"; done > "$W/fx/v4"
for i in $(seq 1 7); do echo "2400:cb0$i::/32"; done > "$W/fx/v6"
printf '<html>Service unavailable</html>\n' > "$W/fx/html"
printf '1.2.3.0/24\n' > "$W/fx/short"
{ cat "$W/fx/v4"; echo '1.1.1.0/24; include /etc/passwd'; echo '999.1.1.1/8'; } > "$W/fx/evil"
printf '%s\r\n' $(cat "$W/fx/v4") > "$W/fx/crlf"

pass=0; failn=0
run() {  # run <v4 fixture> <v6 fixture>; sets rc
  : > "$W/calls"
  CF_CONF_DIR="$W/conf" CF_V4_URL="file://$W/fx/$1" CF_V6_URL="file://$W/fx/$2" bash "$W/run.sh" >/dev/null 2>&1; rc=$?
}
check() {  # check <name> '<shell condition>'
  if eval "$2"; then pass=$((pass + 1)); else failn=$((failn + 1)); echo "FAIL: $1"; fi
}
snap() { cat "$W/conf/"* 2>/dev/null | md5sum; }
reloaded() { grep -q 'systemctl reload nginx' "$W/calls"; }

run v4 v6
check "first run succeeds" '[ "$rc" = 0 ]'
check "first run writes 22 ranges" '[ "$(grep -c "^set_real_ip_from" "$W/conf/cloudflare-realip.conf")" = 22 ]'
check "guard maps every range" '[ "$(grep -c " 1;$" "$W/conf/cloudflare-origin-guard.conf")" = 24 ]'
check "first run reloads" 'reloaded'
good=$(snap)

run v4 v6
check "unchanged ranges: exit 0" '[ "$rc" = 0 ]'
check "unchanged ranges: no reload" '! grep -q reload "$W/calls"'

run missing v6
check "IPv4 download failure: exit 1" '[ "$rc" = 1 ]'
check "IPv4 download failure: files kept" '[ "$(snap)" = "$good" ]'
run v4 missing
check "IPv6 download failure: files kept" '[ "$rc" = 1 ] && [ "$(snap)" = "$good" ]'
run v4 html
check "IPv6 list is HTML: files kept, no reload" '[ "$rc" = 1 ] && [ "$(snap)" = "$good" ] && ! reloaded'
run short v6
check "too few IPv4 ranges: files kept" '[ "$rc" = 1 ] && [ "$(snap)" = "$good" ]'

run evil v6
check "injected line is dropped" '! grep -q passwd "$W/conf/cloudflare-realip.conf" && ! grep -q "999[.]" "$W/conf/cloudflare-realip.conf"'
run crlf v6
check "CRLF lists parse" '[ "$rc" = 0 ] && [ "$(grep -c "^set_real_ip_from" "$W/conf/cloudflare-realip.conf")" = 22 ]'

sed -i '1d' "$W/fx/v4"; echo '104.99.0.0/16' >> "$W/fx/v4"   # a real change
touch "$W/nginx_fails"; before=$(snap)
run v4 v6
check "nginx -t failure: exit 1, previous files restored, no reload" '[ "$rc" = 1 ] && [ "$(snap)" = "$before" ] && ! reloaded'
rm -f "$W/nginx_fails"; touch "$W/reload_fails"
run v4 v6
check "reload failure: previous files restored" '[ "$rc" = 1 ] && [ "$(snap)" = "$before" ]'
rm -f "$W/reload_fails"
run v4 v6
check "changed ranges applied and reloaded" '[ "$rc" = 0 ] && grep -q "104.99.0.0/16" "$W/conf/cloudflare-realip.conf" && reloaded'

rm -f "$W/conf/"*
run v4 html
check "first install with a bad list leaves no files" '[ "$rc" = 1 ] && [ -z "$(ls "$W/conf")" ]'

echo "update_cloudflare_ips.sh tests: $pass passed, $failn failed"
[ "$failn" = 0 ]
