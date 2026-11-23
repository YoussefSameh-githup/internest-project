# AWS EC2 server (Ubuntu 24.04, nginx → gunicorn on 127.0.0.1:8000)

Versioned config for the live EC2 host. Apply after `git pull`:

```bash
sudo bash deploy/aws/apply_nginx.sh              # site + hardening, backup + rollback on a failed `nginx -t`
sudo bash deploy/aws/install_cloudflare_cron.sh  # weekly Cloudflare range refresh (installed; re-run if the script changes)
sudo /usr/local/sbin/update-cloudflare-ips       # refresh now (log: /var/log/update-cloudflare-ips.log)
```

| File | Installs to | Why |
| --- | --- | --- |
| `nginx/internest.conf` | `/etc/nginx/sites-available/internest` | no general `/media/` alias (private uploads go through Django's permission checks; only public company logos are served directly); edge rate limit on auth paths; body/timeout caps |
| `nginx/internest-hardening.conf` | `/etc/nginx/conf.d/` | `server_tokens off`, the `internest_auth` limit zone |
| `update_cloudflare_ips.sh` | `/etc/nginx/conf.d/cloudflare-realip.conf` | trusts `CF-Connecting-IP` only from Cloudflare's published ranges; validated CIDRs, `nginx -t` before reload, restores the old file on failure |
| `install_cloudflare_cron.sh` | `/usr/local/sbin/update-cloudflare-ips`, `/etc/cron.d/update-cloudflare-ips` | runs the refresh every Sunday 03:00 UTC from a root-owned copy |
| `lock_sg_to_cloudflare.sh` | (AWS API, run from your machine) | limits the security group's 80/443 to Cloudflare; see below. **Not applied yet.** |
| `internest-worker.service` | `/etc/systemd/system/` | the background AI worker (`ASYNC_TASK_BACKEND=db`) |

Security headers (CSP with a per-response script nonce, HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy,
nosniff, COOP) come from Django (`internest_core/csp.py`),
not nginx, so they are not duplicated here.

## Putting Cloudflare in front (when the domain resolves)

1. Add the domain to Cloudflare with proxied (orange-cloud) `A` records → `16.171.20.95` for `@` and `www`.
2. SSL/TLS mode **Full (strict)** with a Cloudflare Origin Certificate on nginx (`listen 443 ssl`), never "Flexible":
   Flexible leaves Cloudflare → origin in plain HTTP. Then set `DJANGO_SECURE_SSL_REDIRECT=True`.
3. Restrict the EC2 security group's ports 80/443 to Cloudflare's ranges, so nobody can bypass the edge. Only after
   the site works through Cloudflare (from your machine or AWS CloudShell, with an AWS CLI login):
   ```bash
   bash deploy/aws/lock_sg_to_cloudflare.sh --group-id sg-XXXXXXXX --region eu-north-1            # shows the plan
   bash deploy/aws/lock_sg_to_cloudflare.sh --group-id sg-XXXXXXXX --region eu-north-1 --confirm  # applies it
   ```
   It never touches port 22, adds Cloudflare's ranges before revoking `0.0.0.0/0` / `::/0` on 80/443, is safe to
   re-run, and refuses (exit 3) if an "All traffic" or port-range rule also opens 80/443. After this,
   `http://16.171.20.95/` stops working for visitors, which is the point. Cloudflare's ranges rarely change; re-run the
   script when they do (the nginx cron keeps nginx current, but not the security group).
4. Cloudflare dashboard: WAF managed rules on, Bot Fight Mode on, and a rate-limit rule for `/accounts/login/`
   and `/api/ai/` as an outer layer.
