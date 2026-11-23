# AWS EC2 server (Ubuntu 24.04, nginx → gunicorn on 127.0.0.1:8000)

Versioned config for the live EC2 host. Apply after `git pull`:

```bash
sudo bash deploy/aws/apply_nginx.sh            # site + hardening, backup + rollback on a failed `nginx -t`
sudo bash deploy/aws/update_cloudflare_ips.sh  # real client IPs behind Cloudflare (re-run weekly, e.g. cron)
```

| File | Installs to | Why |
| --- | --- | --- |
| `nginx/internest.conf` | `/etc/nginx/sites-available/internest` | no general `/media/` alias (private uploads go through Django's permission checks; only public company logos are served directly); edge rate limit on auth paths; body/timeout caps |
| `nginx/internest-hardening.conf` | `/etc/nginx/conf.d/` | `server_tokens off`, the `internest_auth` limit zone |
| `update_cloudflare_ips.sh` | `/etc/nginx/conf.d/cloudflare-realip.conf` | trusts `CF-Connecting-IP` only from Cloudflare's published ranges |
| `internest-worker.service` | `/etc/systemd/system/` | the background AI worker (`ASYNC_TASK_BACKEND=db`) |

Security headers (CSP, HSTS, X-Frame-Options, Referrer-Policy, Permissions-Policy, nosniff, COOP) come from Django,
not nginx, so they are not duplicated here.

## Putting Cloudflare in front (when the domain resolves)

1. Add the domain to Cloudflare with proxied (orange-cloud) `A` records → `16.171.20.95` for `@` and `www`.
2. SSL/TLS mode **Full (strict)** with a Cloudflare Origin Certificate on nginx (`listen 443 ssl`), never "Flexible":
   Flexible leaves Cloudflare → origin in plain HTTP. Then set `DJANGO_SECURE_SSL_REDIRECT=True`.
3. Restrict the EC2 security group's ports 80/443 to Cloudflare's ranges, so nobody can bypass the edge.
4. Cloudflare dashboard: WAF managed rules on, Bot Fight Mode on, and a rate-limit rule for `/accounts/login/`
   and `/api/ai/` as an outer layer.
