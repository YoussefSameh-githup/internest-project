#!/usr/bin/env bash
# Restrict an EC2 security group's inbound TCP 80 and 443 to Cloudflare's published edge ranges, so nobody can reach
# the origin around Cloudflare's WAF, rate limits and DDoS protection. Port 22 (SSH) and every other port are never
# touched.
#
#   bash deploy/aws/lock_sg_to_cloudflare.sh --group-id sg-0123456789abcdef0 [--region eu-north-1]            # plan only
#   bash deploy/aws/lock_sg_to_cloudflare.sh --group-id sg-0123456789abcdef0 [--region eu-north-1] --confirm  # apply
#
# ONLY RUN --confirm AFTER the domain is proxied by Cloudflare (orange cloud) and works over it: afterwards
# http://<server-ip>/ and any DNS-only (grey cloud) record stop working for visitors.
#
# Idempotent: it adds the Cloudflare ranges that are missing and revokes every other IPv4/IPv6 CIDR on exactly
# tcp/80 and tcp/443 (0.0.0.0/0, ::/0, old ranges). A second run with nothing to change does nothing. New rules are
# added BEFORE old ones are revoked, so 80/443 are never closed to Cloudflare while it runs.
# It refuses to apply (exit 3) when another rule also opens 80 or 443, e.g. "All traffic" or a 0-65535 range: such a
# rule usually also carries SSH, so the script never edits it; fix it by hand first.
# Rules on 80/443 that reference another security group or a prefix list (a load balancer) are reported, not touched.
#
# Needs: aws CLI v2 with credentials allowed to ec2:DescribeSecurityGroups, ec2:AuthorizeSecurityGroupIngress and
# ec2:RevokeSecurityGroupIngress; curl; python3. Run it from your own machine or AWS CloudShell, not via the website.
set -euo pipefail

GROUP="" REGION="" CONFIRM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --group-id) GROUP="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --confirm) CONFIRM=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ "$GROUP" =~ ^sg-[0-9a-f]{8,17}$ ]] || { echo "--group-id sg-… is required (see --help)" >&2; exit 2; }
for tool in aws curl python3; do command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 2; }; done

AWS=(aws ec2 --output json)
[ -n "$REGION" ] && AWS+=(--region "$REGION")

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4 -o "$WORK/v4"
curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6 -o "$WORK/v6"
"${AWS[@]}" describe-security-groups --group-ids "$GROUP" > "$WORK/sg.json"

# Plan: writes add.json / revoke.json (EC2 IpPermissions lists) and prints a summary; exit 3 = unsafe to apply.
set +e
python3 - "$WORK" <<'PY'
import ipaddress, json, pathlib, sys

work = pathlib.Path(sys.argv[1])
PORTS = (80, 443)
DESCRIPTION = "Cloudflare edge (lock_sg_to_cloudflare.sh)"


def ranges(name, version):
    nets = []
    for line in (work / name).read_text().split():
        net = ipaddress.ip_network(line.strip(), strict=True)  # raises on anything that is not a clean CIDR
        if net.version != version:
            raise SystemExit(f"unexpected address family in {name}: {line}")
        nets.append(str(net))
    if len(nets) < 5:
        raise SystemExit(f"only {len(nets)} Cloudflare ranges in {name}; refusing to continue")
    return sorted(set(nets))


cf4, cf6 = ranges("v4", 4), ranges("v6", 6)
group = json.loads((work / "sg.json").read_text())["SecurityGroups"][0]

broad, references = [], []
current = {port: {"v4": set(), "v6": set()} for port in PORTS}
for perm in group.get("IpPermissions", []):
    proto, lo, hi = perm.get("IpProtocol"), perm.get("FromPort"), perm.get("ToPort")
    exact = proto == "tcp" and lo == hi and lo in PORTS
    covers = proto == "-1" or (proto == "tcp" and lo is not None and any(lo <= p <= hi for p in PORTS))
    if covers and not exact:
        broad.append("all traffic" if proto == "-1" else f"{proto} {lo}-{hi}")
        continue
    if not exact:
        continue  # SSH (22) and every other port: never touched
    current[lo]["v4"].update(r["CidrIp"] for r in perm.get("IpRanges", []))
    current[lo]["v6"].update(r["CidrIpv6"] for r in perm.get("Ipv6Ranges", []))
    if perm.get("UserIdGroupPairs") or perm.get("PrefixListIds"):
        references.append(f"tcp/{lo}")


def permissions(port, v4, v6, describe):
    perm = {"IpProtocol": "tcp", "FromPort": port, "ToPort": port}
    if v4:
        perm["IpRanges"] = [dict({"CidrIp": c}, **({"Description": DESCRIPTION} if describe else {})) for c in sorted(v4)]
    if v6:
        perm["Ipv6Ranges"] = [dict({"CidrIpv6": c}, **({"Description": DESCRIPTION} if describe else {})) for c in sorted(v6)]
    return perm


add, revoke = [], []
print(f"Security group {group['GroupId']} ({group.get('GroupName', '')}); Cloudflare: {len(cf4)} IPv4 + {len(cf6)} IPv6 ranges")
for port in PORTS:
    add4, add6 = set(cf4) - current[port]["v4"], set(cf6) - current[port]["v6"]
    rev4, rev6 = current[port]["v4"] - set(cf4), current[port]["v6"] - set(cf6)
    if add4 or add6:
        add.append(permissions(port, add4, add6, True))
    if rev4 or rev6:
        revoke.append(permissions(port, rev4, rev6, False))
    print(f"  tcp/{port}: add {len(add4) + len(add6)} Cloudflare range(s); revoke {len(rev4) + len(rev6)} other CIDR(s)"
          + (f" -> {', '.join(sorted(rev4 | rev6))}" if rev4 or rev6 else ""))
print("  tcp/22 and all other ports: unchanged")
for ref in references:
    print(f"  NOTE: {ref} also allows a security group or prefix list (e.g. a load balancer); left as it is.")
(work / "add.json").write_text(json.dumps(add))
(work / "revoke.json").write_text(json.dumps(revoke))
if broad:
    print("  REFUSING: these rules also open 80/443 and may carry SSH, so this script will not edit them: "
          + "; ".join(broad) + ". Replace them with explicit port rules by hand, then re-run.")
    sys.exit(3)
sys.exit(0 if (add or revoke) else 4)
PY
plan=$?
set -e

case "$plan" in
  0) ;;
  4) echo "Already locked to Cloudflare: nothing to change."; exit 0 ;;
  3) exit 3 ;;
  *) echo "Planning failed (exit $plan); nothing was changed." >&2; exit 1 ;;
esac

if [ "$CONFIRM" -ne 1 ]; then
  echo "Plan only. Re-run with --confirm to apply (only once the site works through Cloudflare)."
  exit 0
fi

if [ "$(cat "$WORK/add.json")" != "[]" ]; then
  "${AWS[@]}" authorize-security-group-ingress --group-id "$GROUP" --ip-permissions "file://$WORK/add.json" >/dev/null
  echo "Added the missing Cloudflare ranges."
fi
if [ "$(cat "$WORK/revoke.json")" != "[]" ]; then
  "${AWS[@]}" revoke-security-group-ingress --group-id "$GROUP" --ip-permissions "file://$WORK/revoke.json" >/dev/null
  echo "Revoked the non-Cloudflare CIDRs on 80/443."
fi
echo "Done: tcp/80 and tcp/443 accept Cloudflare only; SSH (22) untouched."
