"""Point an existing .env at this server: add the domain to the allowed hosts and CSRF origins, set the backup folder
and the HTTPS redirect. Every other line (secret key, API keys, comments) is kept exactly as it was.

    python configure_env.py /srv/internest/app/.env --domain internest.app --tls 1 --backup-dir /srv/internest/backups/internest
"""
import argparse
import re
from pathlib import Path


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def configure(text: str, domain: str, tls: bool, backup_dir: str) -> str:
    lines = text.splitlines()
    values = {}
    for line in lines:
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")

    hosts = [h for h in _split(values.get("DJANGO_ALLOWED_HOSTS", "")) if h]
    hosts = [domain] + [h for h in hosts if h != domain]
    for local in ("127.0.0.1", "localhost"):
        if local not in hosts:
            hosts.append(local)
    origin = f"https://{domain}"
    origins = [origin] + [o for o in _split(values.get("DJANGO_CSRF_TRUSTED_ORIGINS", "")) if o != origin]
    wanted = {
        "DJANGO_ALLOWED_HOSTS": ",".join(hosts),
        "DJANGO_CSRF_TRUSTED_ORIGINS": ",".join(origins),
        "DJANGO_SECURE_SSL_REDIRECT": "True" if tls else "False",
        "BACKUP_DIR": backup_dir,
    }
    out, done = [], set()
    for line in lines:
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        key = match.group(1) if match else None
        if key in wanted:
            if key not in done:
                out.append(f"{key}={wanted[key]}")
                done.add(key)
            continue  # a duplicate of a managed key: dropped, the first one wins
        out.append(line)
    missing = [key for key in wanted if key not in done]
    if missing:
        out += ["", "# --- this server (written by deploy/azure/configure_env.py) ---"]
        out += [f"{key}={wanted[key]}" for key in missing]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file")
    parser.add_argument("--domain", required=True)
    parser.add_argument("--tls", type=int, choices=(0, 1), default=1)
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args()
    path = Path(args.env_file)
    path.write_text(configure(path.read_text(encoding="utf-8"), args.domain, bool(args.tls), args.backup_dir), encoding="utf-8")
    print(f"{path}: hosts, CSRF origins, HTTPS redirect and backup folder set for {args.domain}.")


if __name__ == "__main__":
    main()
