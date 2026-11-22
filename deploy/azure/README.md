# Internest on an Azure for Students VM

One Ubuntu 24.04 VM runs everything: nginx → gunicorn → Django, SQLite on the VM's disk, a daily backup timer and a
free HTTPS certificate. The same scripts work on any Ubuntu 24.04 VPS. They are tested on every change by
`.github/workflows/server-setup.yml` (full install, security checks, backup + restore, update).

## 1. Create the VM (Azure portal)

1. **Find the regions your student subscription allows.** Portal search bar → **Policy** → **Assignments** →
   **Allowed resource deployment regions** → **Parameters**. Only those regions work (usually about five, different per
   student). Using any other region is what gives `RequestDisallowedByAzure`.
2. **Virtual machines** → **Create** → **Azure virtual machine**:
   - Region: one from the list above. Availability: *No infrastructure redundancy required*.
   - Image: **Ubuntu Server 24.04 LTS - x64 Gen2**.
   - Size: a free-tier size, **B2ats_v2** (or B1s). If it says the size is not available, try the next free size,
     then the next allowed region (`SkuNotAvailable` is about capacity, not your account).
   - Authentication: **SSH public key**, username `azureuser`, *Generate new key pair*; download the `.pem` file.
   - Inbound ports: **SSH (22)**, **HTTP (80)**, **HTTPS (443)**.
   - Disks: **Standard SSD**, 64 GB. Networking: keep the new public IP (Standard, static).
3. **Create**, then copy the VM's **Public IP address**.

## 2. Point the domain at it

At your registrar's DNS page, add an **A record**: host `@`, value `<the VM's public IP>`. Keep the old record until
step 5 if the site is live on PythonAnywhere today.

## 3. Install

```powershell
ssh -i C:\path\to\key.pem azureuser@<VM IP>
```
On the VM:
```bash
git clone https://github.com/YoussefSameh-githup/internest-project.git /tmp/internest
sudo bash /tmp/internest/deploy/azure/setup.sh --domain internest.app --email you@example.com --no-tls
```
It installs everything and stops, asking for the `.env`.

## 4. Bring the data and settings from PythonAnywhere

1. PythonAnywhere **Bash console**: `cd ~/internest-project && python manage.py backup_data`
2. **Files** tab: download `~/internest-project/.env` and the newest `~/backups/internest/internest-full-*.tar.gz`.
3. From your PC (PowerShell, in the download folder):
   ```powershell
   scp -i C:\path\to\key.pem .env internest-full-XXXX.tar.gz azureuser@<VM IP>:/tmp/
   ```
4. On the VM:
   ```bash
   sudo mv /tmp/.env /srv/internest/app/.env
   sudo bash /srv/internest/app/deploy/azure/setup.sh --domain internest.app --email you@example.com --no-tls
   sudo systemctl stop internest
   sudo -u internest -H bash -c "cd /srv/internest/app && /srv/internest/venv/bin/python manage.py restore_backup /tmp/internest-full-XXXX.tar.gz --yes"
   sudo systemctl start internest
   ```

## 5. HTTPS and go live

Once the A record points at the VM (check: `nslookup internest.app` shows the VM IP), run the setup again **without**
`--no-tls`:
```bash
sudo bash /srv/internest/app/deploy/azure/setup.sh --domain internest.app --email you@example.com
```
It ends with `https://internest.app is live.` The certificate renews itself.

Then, outside the server:
- Google / LinkedIn / GitHub / Microsoft sign-in consoles: add `https://internest.app/oauth/<provider>/login/callback/`
  to the allowed redirect URIs (keep the old ones until the switch is done).
- Django admin → **Sites**: set the domain to `internest.app`.
- Payment gateway (Fawaterak): update the callback / webhook URLs to the new domain.
- SendGrid: authenticate the domain (SPF/DKIM DNS records) so codes reach the inbox.
- Sentry (if used): nothing to change.

## Every update after that

```bash
sudo bash /srv/internest/app/deploy/azure/update.sh
```
(pull, requirements, migrate, collectstatic, restart, health check). Commit `.mo` files as usual: the server does not
compile translations, so its checkout never gets local changes.

## Day to day

| What | Command |
| --- | --- |
| App status / logs | `sudo systemctl status internest` · `sudo journalctl -u internest -n 100` |
| Daily jobs (02:00 UTC) | `systemctl list-timers internest-daily` · `sudo journalctl -u internest-daily -n 50` |
| Run them now | `sudo systemctl start internest-daily` |
| Backups | `/srv/internest/backups/internest/` (14 kept). Copy one off the VM now and then: `scp -i key.pem azureuser@<IP>:/srv/internest/backups/internest/<bundle> .` |
| Django shell | `sudo -u internest -H bash -c "cd /srv/internest/app && /srv/internest/venv/bin/python manage.py shell"` |

## What it costs

The free-tier VM size, disk and outbound traffic come with the student offer; the static public IPv4 address is
billed from the credit (a few dollars a month). Check **Cost Management** in the portal monthly, and set a budget alert
there. When the credit or the student status ends, the VM keeps billing a normal card only if you upgrade the
subscription; otherwise it is disabled, so take a backup off the VM before then.

## Security built in

nginx never serves `/media/` itself (CVs, receipts and certificates stay behind `serve_protected_media`), overwrites
`X-Real-IP` (the rate limiter trusts it) and drops requests for unknown host names; gunicorn runs as its own user with a
read-only system; `.env` is `600`; the firewall allows SSH, HTTP and HTTPS only; fail2ban blocks SSH brute force;
security updates install automatically.
