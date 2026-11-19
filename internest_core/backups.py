"""Backups of the SQLite database and uploaded files (Phase 8). Used by `backup_data`, `verify_backup`, `restore_backup`.

One backup = one `internest-<UTC timestamp>.tar.gz` bundle in BACKUP_DIR (outside the project, never under /media/ or
/static/, so the web server cannot serve it):

    manifest.json   created_at, Django migration state, file list with SHA-256 and size
    db.sqlite3      a consistent copy made with SQLite's online backup API (safe while the site is running)
    media/...       every uploaded file (CVs, receipts, certificates...), unless --no-media

Bundles hold private data (CVs, payment receipts, emails): files and folder are created owner-only (0600 / 0700),
and the bundle must never be uploaded anywhere public. Off-site copies are the owner's job (download it to an
encrypted disk); nothing here sends data to a third party.

Restore checks every checksum and SQLite's integrity_check BEFORE touching anything, keeps a copy of the current
database next to it, refuses archive members that would escape the media folder, and never deletes existing media.
"""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import connection

BUNDLE_PREFIX = "internest-"
BUNDLE_SUFFIX = ".tar.gz"
MANIFEST = "manifest.json"
DB_MEMBER = "db.sqlite3"
MEDIA_PREFIX = "media/"
FORMAT_VERSION = 1


class BackupError(Exception):
    pass


def backup_dir() -> Path:
    return Path(getattr(settings, "BACKUP_DIR", "") or Path.home() / "backups" / "internest")


def _private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # Windows: permissions are handled by the user profile
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:  # `with sqlite3.connect()` only commits; an unclosed handle keeps the file locked on Windows
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def _copy_database(dest: Path):
    """Online backup through Django's own connection: consistent even while other workers write."""
    if connection.vendor != "sqlite":
        raise BackupError("backup_data handles SQLite only; use the database provider's backups for anything else.")
    target = sqlite3.connect(dest)
    try:
        if connection.is_in_memory_db():
            connection.ensure_connection()
            connection.connection.backup(target)
        else:  # a separate read connection: never shares the web request's transaction, waits up to 30 s for a lock
            source = sqlite3.connect(str(connection.settings_dict["NAME"]), timeout=30)
            try:
                source.backup(target, pages=1024)  # in steps, so writers are not blocked for the whole copy
            finally:
                source.close()
    finally:
        target.close()


def _migration_state():
    from django.db.migrations.recorder import MigrationRecorder
    rows = MigrationRecorder(connection).migration_qs.values_list("app", "name")
    latest = {}
    for app, name in rows:
        latest[app] = max(latest.get(app, ""), name)
    return dict(sorted(latest.items()))


def _media_files(media_root: Path):
    if not media_root.is_dir():
        return []
    files = []
    for root, _dirs, names in os.walk(media_root):
        for name in names:
            path = Path(root) / name
            if path.is_file() and not path.is_symlink():
                files.append(path)
    return sorted(files)


def _check_location(dest_dir: Path):
    """Bundles hold every CV and receipt: never inside the project (git, deploys), MEDIA_ROOT or STATIC_ROOT (served)."""
    dest = Path(os.path.realpath(dest_dir))
    for label, root in (("the project folder", settings.BASE_DIR), ("MEDIA_ROOT", settings.MEDIA_ROOT),
                        ("STATIC_ROOT", getattr(settings, "STATIC_ROOT", None))):
        if root and (dest == Path(os.path.realpath(root)) or Path(os.path.realpath(root)) in dest.parents):
            raise BackupError(f"Refusing to write backups inside {label} ({root}). Set BACKUP_DIR outside it.")


def create_backup(include_media=True, dest_dir=None) -> Path:
    dest_dir = Path(dest_dir) if dest_dir else backup_dir()
    _check_location(dest_dir)
    _private_dir(dest_dir)
    stamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle = dest_dir / f"{BUNDLE_PREFIX}{stamp}{BUNDLE_SUFFIX}"
    n = 1
    while bundle.exists():  # two backups in the same second
        bundle = dest_dir / f"{BUNDLE_PREFIX}{stamp}-{n}{BUNDLE_SUFFIX}"
        n += 1
    media_root = Path(settings.MEDIA_ROOT)

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / DB_MEMBER
        _copy_database(db_copy)
        check = _integrity(db_copy)
        if check != "ok":
            raise BackupError(f"The live database failed integrity_check: {check}")
        entries = [{"name": DB_MEMBER, "sha256": _sha256(db_copy), "size": db_copy.stat().st_size}]
        media = _media_files(media_root) if include_media else []
        for path in media:
            rel = path.relative_to(media_root).as_posix()
            entries.append({"name": MEDIA_PREFIX + rel, "sha256": _sha256(path), "size": path.stat().st_size})
        manifest = {"format": FORMAT_VERSION, "created_at": stamp, "include_media": include_media,
                    "migrations": _migration_state(), "files": entries}

        partial = bundle.with_name(bundle.name + ".partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime, info.mode = len(data), int(datetime.now().timestamp()), 0o600
            tar.addfile(info, io.BytesIO(data))
            tar.add(db_copy, arcname=DB_MEMBER, recursive=False)
            for path in media:
                tar.add(path, arcname=MEDIA_PREFIX + path.relative_to(media_root).as_posix(), recursive=False)
        os.replace(partial, bundle)  # a crash mid-write never leaves a bundle that looks complete
    return bundle


def _safe_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        return False
    parts = name.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def verify_backup(bundle) -> dict:
    """Open the bundle, check every listed file's SHA-256 and size, the database's integrity_check, and that nothing
    unlisted or unsafe is inside. Returns the manifest. Raises BackupError on the first problem."""
    bundle = Path(bundle)
    if not bundle.is_file():
        raise BackupError(f"No such backup: {bundle}")
    with tarfile.open(bundle, "r:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        members = {m.name: m for m in tar.getmembers()}
        if MANIFEST not in members:
            raise BackupError("manifest.json is missing")
        manifest = json.load(tar.extractfile(members[MANIFEST]))
        listed = {e["name"]: e for e in manifest.get("files", [])}
        for name, member in members.items():
            if name == MANIFEST:
                continue
            if not member.isfile() or not _safe_member(name) or name not in listed:
                raise BackupError(f"Unexpected or unsafe archive member: {name!r}")
        for name, entry in listed.items():
            member = members.get(name)
            if member is None:
                raise BackupError(f"Listed file missing from the archive: {name}")
            digest, size = hashlib.sha256(), 0
            src = tar.extractfile(member)
            if name == DB_MEMBER:
                db_path = Path(tmp) / DB_MEMBER
                with open(db_path, "wb") as out:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
            else:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != entry["sha256"] or size != entry["size"]:
                raise BackupError(f"Checksum mismatch: {name}")
        if DB_MEMBER not in listed:
            raise BackupError("The database is missing from the backup")
        check = _integrity(Path(tmp) / DB_MEMBER)
        if check != "ok":
            raise BackupError(f"The backed-up database failed integrity_check: {check}")
    return manifest


def restore_backup(bundle, db_path=None, media_root=None, include_media=True) -> dict:
    """Replace the database (and add/overwrite media files) from a verified bundle. Run with the web app stopped.

    The current database is first copied to `<db>.before-restore-<timestamp>`; media files that are not in the bundle
    are left alone (a restore never deletes uploads)."""
    manifest = verify_backup(bundle)
    db_path = Path(db_path or settings.DATABASES["default"]["NAME"])
    media_root = Path(media_root or settings.MEDIA_ROOT)
    stamp = datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
    safety_copy = None
    live = str(connection.settings_dict.get("NAME")) == str(db_path)
    if live:
        connection.close()  # release our own handle on the file we are about to replace
    if db_path.exists():
        safety_copy = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
        src, dst = sqlite3.connect(db_path), sqlite3.connect(safety_copy)
        try:
            src.backup(dst)  # includes anything still in the WAL file
        finally:
            src.close()
            dst.close()
    for suffix in ("-wal", "-shm", "-journal"):
        # A WAL left over from the old database would be replayed into the restored one and corrupt it.
        leftover = db_path.with_name(db_path.name + suffix)
        if leftover.exists():
            leftover.unlink()
    restored_files = 0
    with tarfile.open(bundle, "r:gz") as tar:
        incoming = db_path.with_name(db_path.name + ".restoring")
        with tar.extractfile(DB_MEMBER) as src, open(incoming, "wb") as out:
            shutil.copyfileobj(src, out)
        os.replace(incoming, db_path)
        if include_media:
            media_real = os.path.realpath(media_root)
            for member in tar.getmembers():
                if not member.name.startswith(MEDIA_PREFIX):
                    continue
                rel = member.name[len(MEDIA_PREFIX):]
                target = os.path.realpath(os.path.join(media_real, *rel.split("/")))
                if not target.startswith(media_real + os.sep):
                    raise BackupError(f"Refusing to write outside MEDIA_ROOT: {member.name!r}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                restored_files += 1
    return {"manifest": manifest, "safety_copy": safety_copy, "media_files": restored_files}


def list_backups(dest_dir=None):
    dest_dir = Path(dest_dir) if dest_dir else backup_dir()
    if not dest_dir.is_dir():
        return []
    bundles = [p for p in dest_dir.glob(f"{BUNDLE_PREFIX}*{BUNDLE_SUFFIX}") if p.is_file()]
    return sorted(bundles, key=lambda p: (p.stat().st_mtime, p.name))  # oldest first


def prune_backups(keep: int, dest_dir=None):
    """Keep the newest `keep` bundles; delete older ones. Returns the deleted paths."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    bundles = list_backups(dest_dir)
    old = bundles[:-keep]
    for path in old:
        path.unlink()
    return old
