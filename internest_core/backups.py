"""Backups of the SQLite database and uploaded files (Phase 8). Used by `backup_data`, `verify_backup`, `restore_backup`.

One backup = one bundle in BACKUP_DIR (an absolute path outside the project, MEDIA_ROOT and STATIC_ROOT):

    internest-full-<UTC time>.tar.gz   database + every media file
    internest-db-<UTC time>.tar.gz     database only (--no-media); pruned separately, so frequent database-only
                                       backups never push the full ones out

Inside: `db.sqlite3` (SQLite online-backup copy, consistent while the site runs, journal mode reset to DELETE so it
is safe on PythonAnywhere's network disk), `media/...`, and `manifest.json` LAST (each file's SHA-256 and size are
computed from the exact bytes written into the archive, so a file changing mid-backup cannot make the bundle lie).

Bundles hold private data (CVs, receipts, emails, password hashes): owner-only files (0600) and folder (0700); never
upload them anywhere public. Nothing here sends data to a third party.

Restore = verify everything first (checksums, sizes, duplicate/unsafe members, every media target path, database
integrity, migrations not newer than the code) → refuse if any other process still has the database open → keep a
safety copy in BACKUP_DIR → atomically replace the database → write media. Existing media files are never deleted.
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

BUNDLE_SUFFIX = ".tar.gz"
KINDS = {"full": "internest-full-", "db": "internest-db-"}
MANIFEST = "manifest.json"
DB_MEMBER = "db.sqlite3"
MEDIA_PREFIX = "media/"
FORMAT_VERSION = 2
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- locations and helpers
def backup_dir() -> Path:
    return Path(getattr(settings, "BACKUP_DIR", "") or Path.home() / "backups" / "internest")


def _check_location(dest_dir: Path):
    """Bundles hold every CV and receipt: never inside the project (git, deploys), MEDIA_ROOT or STATIC_ROOT (served),
    and never the home folder itself (we chmod the folder to 0700)."""
    if not Path(dest_dir).is_absolute():
        raise BackupError(f"BACKUP_DIR must be an absolute path, got {dest_dir!r}.")
    dest = Path(os.path.realpath(dest_dir))
    if dest == Path(os.path.realpath(Path.home())):
        raise BackupError("Use a sub-folder such as ~/backups/internest, not the home folder itself.")
    for label, root in (("the project folder", settings.BASE_DIR), ("MEDIA_ROOT", settings.MEDIA_ROOT),
                        ("STATIC_ROOT", getattr(settings, "STATIC_ROOT", None))):
        if root:
            real = Path(os.path.realpath(root))
            if dest == real or real in dest.parents:
                raise BackupError(f"Refusing to write backups inside {label} ({root}). Set BACKUP_DIR outside it.")


def _private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # Windows: permissions come from the user profile
        pass


def _stamp():
    return datetime.now(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")


def _integrity(db_path: Path) -> str:
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        return f"cannot open: {exc}"
    try:  # `with sqlite3.connect()` only commits; an unclosed handle keeps the file locked on Windows
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:  # "file is not a database"
        return str(exc)
    finally:
        conn.close()


def _copy_database(dest: Path):
    """Online backup: consistent even while other workers write. The copy is switched to journal_mode=DELETE so a
    bundle made on a WAL machine never brings WAL onto a network filesystem."""
    if connection.vendor != "sqlite":
        raise BackupError("backup_data handles SQLite only; use the database provider's backups for anything else.")
    target = sqlite3.connect(dest)
    try:
        if connection.is_in_memory_db():
            connection.ensure_connection()
            connection.connection.backup(target)
        else:  # its own read connection: never shares a request's transaction; waits up to 30 s for a lock
            source = sqlite3.connect(str(connection.settings_dict["NAME"]), timeout=30)
            try:
                source.backup(target, pages=1024)  # in steps, so writers are not blocked for the whole copy
            finally:
                source.close()
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()


def _migration_state(conn=None):
    from django.db.migrations.recorder import MigrationRecorder
    rows = MigrationRecorder(conn or connection).migration_qs.values_list("app", "name")
    applied = {}
    for app, name in rows:
        applied.setdefault(app, []).append(name)
    return {app: sorted(names) for app, names in sorted(applied.items())}


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


def _add_bytes(tar, name, data: bytes):
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), int(datetime.now().timestamp()), 0o600
    tar.addfile(info, io.BytesIO(data))
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


# ---------------------------------------------------------------- create
def create_backup(include_media=True, dest_dir=None) -> Path:
    dest_dir = Path(dest_dir) if dest_dir else backup_dir()
    _check_location(dest_dir)
    _private_dir(dest_dir)
    kind = "full" if include_media else "db"
    stamp = _stamp()
    bundle = dest_dir / f"{KINDS[kind]}{stamp}{BUNDLE_SUFFIX}"
    n = 1
    while bundle.exists():  # two backups in the same second
        bundle = dest_dir / f"{KINDS[kind]}{stamp}-{n}{BUNDLE_SUFFIX}"
        n += 1
    media_root = Path(settings.MEDIA_ROOT)
    partial = bundle.with_name(bundle.name + ".partial")

    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / DB_MEMBER
        _copy_database(db_copy)
        check = _integrity(db_copy)
        if check != "ok":
            raise BackupError(f"The live database failed integrity_check: {check}")
        entries, skipped = [], []
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
                entries.append(_add_bytes(tar, DB_MEMBER, db_copy.read_bytes()))
                for path in _media_files(media_root) if include_media else []:
                    try:
                        data = path.read_bytes()  # hashed from the very bytes archived (uploads are ≤ 5 MB)
                    except OSError:  # deleted or replaced while we walked the folder
                        skipped.append(path.relative_to(media_root).as_posix())
                        continue
                    entries.append(_add_bytes(tar, MEDIA_PREFIX + path.relative_to(media_root).as_posix(), data))
                manifest = {"format": FORMAT_VERSION, "kind": kind, "created_at": stamp,
                            "migrations": _migration_state(), "files": entries, "skipped": skipped}
                _add_bytes(tar, MANIFEST, json.dumps(manifest, indent=2).encode())
            os.replace(partial, bundle)  # a crash mid-write never leaves a bundle that looks complete
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return bundle


# ---------------------------------------------------------------- verify
def _safe_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        return False
    return all(p not in ("", ".", "..") for p in name.split("/"))


def _media_target(media_root: Path, name: str) -> str:
    media_real = os.path.realpath(media_root)
    target = os.path.realpath(os.path.join(media_real, *name[len(MEDIA_PREFIX):].split("/")))
    if not target.startswith(media_real + os.sep):
        raise BackupError(f"Refusing to write outside MEDIA_ROOT: {name!r}")
    return target


def verify_backup(bundle, extract_db_to=None, media_root=None) -> dict:
    """Check the whole bundle and return its manifest. Raises BackupError on the first problem.

    Every member must be a regular file, listed once in the manifest, with a safe name, matching SHA-256 and size; the
    database must pass integrity_check. With `extract_db_to`, the verified database bytes are written there (restore
    then uses exactly what was checked). With `media_root`, every media target path is checked too."""
    bundle = Path(bundle)
    if not bundle.is_file():
        raise BackupError(f"No such backup: {bundle}")
    try:
        with tarfile.open(bundle, "r:gz") as tar, tempfile.TemporaryDirectory() as tmp:
            members = tar.getmembers()
            names = [m.name for m in members]
            duplicates = {n for n in names if names.count(n) > 1}
            if duplicates:
                raise BackupError(f"Archive lists the same name twice: {sorted(duplicates)[:3]}")
            by_name = {m.name: m for m in members}
            for member in members:
                if not member.isfile() or not _safe_member(member.name):  # links, devices, folders, ../
                    raise BackupError(f"Unexpected or unsafe archive member: {member.name!r}")
            if MANIFEST not in by_name:
                raise BackupError("manifest.json is missing")
            try:
                manifest = json.load(tar.extractfile(by_name[MANIFEST]))
            except ValueError as exc:
                raise BackupError(f"manifest.json is unreadable: {exc}") from exc
            listed = [e.get("name") for e in manifest.get("files", [])]
            if len(listed) != len(set(listed)) or set(listed) != set(names) - {MANIFEST}:
                raise BackupError("The manifest and the archive contents differ")
            if DB_MEMBER not in listed:
                raise BackupError("The database is missing from the backup")
            db_path = Path(extract_db_to) if extract_db_to else Path(tmp) / DB_MEMBER
            for entry in manifest["files"]:
                name = entry["name"]
                if name != DB_MEMBER and not name.startswith(MEDIA_PREFIX):
                    raise BackupError(f"Unexpected file in the backup: {name!r}")
                if media_root is not None and name.startswith(MEDIA_PREFIX):
                    _media_target(media_root, name)
                digest, size = hashlib.sha256(), 0
                src = tar.extractfile(by_name[name])
                out = open(db_path, "wb") if name == DB_MEMBER else None
                try:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        if out:
                            out.write(chunk)
                finally:
                    if out:
                        out.flush()
                        os.fsync(out.fileno())
                        out.close()
                if digest.hexdigest() != entry.get("sha256") or size != entry.get("size"):
                    raise BackupError(f"Checksum mismatch: {name}")
            check = _integrity(db_path)
            if check != "ok":
                raise BackupError(f"The backed-up database failed integrity_check: {check}")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"The bundle is unreadable: {exc}") from exc
    return manifest


# ---------------------------------------------------------------- restore
def _processes_holding(db_path: Path) -> list:
    """Best effort, Linux only: other processes of this user with the database file open (/proc/<pid>/fd). Processes on
    other machines (PythonAnywhere runs web workers on separate servers) cannot be seen, so the docs still say: stop them."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    try:
        wanted = os.stat(db_path)
    except OSError:
        return []
    holders = []
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                st = os.stat(fd)
                if (st.st_dev, st.st_ino) == (wanted.st_dev, wanted.st_ino):
                    holders.append(int(pid_dir.name))
                    break
        except OSError:  # not ours to look at, or it exited meanwhile
            continue
    return holders


def _unknown_migrations(manifest) -> list:
    """Migrations recorded in the bundle that this code does not have (a bundle from newer code)."""
    from django.db.migrations.loader import MigrationLoader
    known = set(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)
    unknown = []
    for app, names in (manifest.get("migrations") or {}).items():
        for name in names if isinstance(names, list) else [names]:
            if (app, name) not in known and app in {a for a, _ in known}:
                unknown.append(f"{app}.{name}")
    return unknown


def _assert_not_in_use(db_path: Path, force_side_files=False, safety_dir=None, stamp=""):
    """No other process may hold the database: their writes would go to the replaced file (lost), and their journal
    or WAL could be replayed into the restored one. Checks (best effort): a visible process holding the file (Linux),
    an exclusive lock, and leftover -wal/-journal files. An idle connection on another machine cannot be detected."""
    if not db_path.exists():
        return
    holders = _processes_holding(db_path)
    if holders:
        raise BackupError(f"The database is in use by process(es) {holders[:5]}. Stop the web app and tasks first.")
    # Side files first, before SQLite opens the file: opening it would replay or discard a leftover journal by itself.
    left = [db_path.with_name(db_path.name + sfx) for sfx in SIDE_SUFFIXES if db_path.with_name(db_path.name + sfx).exists()]
    if left and not force_side_files:
        raise BackupError(f"Another process may still have the database open ({', '.join(p.name for p in left)} present). "
                          "Stop it first; if nothing is running (e.g. after a crash), run again with --force-side-files.")
    for side in left:  # kept next to the safety copy: they hold the last transactions of the old database
        _private_dir(Path(safety_dir))
        shutil.move(str(side), str(Path(safety_dir) / f"before-restore-{stamp}{side.name[len(db_path.name):]}"))
    conn = sqlite3.connect(db_path, timeout=2)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise BackupError("The database is in use. Disable the web app and stop the always-on and scheduled tasks "
                          f"(run_async_tasks, backups), then try again. ({exc})") from exc
    except sqlite3.DatabaseError:  # a corrupt file cannot be locked through SQLite
        pass
    finally:
        conn.close()


def _safety_copy(db_path: Path, dest_dir: Path, stamp: str):
    if not db_path.exists():
        return None
    _check_location(dest_dir)
    _private_dir(dest_dir)
    copy = dest_dir / f"before-restore-{stamp}.sqlite3"
    try:
        src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    except sqlite3.DatabaseError:  # the live file is corrupt (often why we restore): keep its raw bytes
        copy.unlink(missing_ok=True)
        copy = dest_dir / f"before-restore-{stamp}.raw"
        shutil.copy2(db_path, copy)
    try:
        os.chmod(copy, 0o600)
    except OSError:
        pass
    return copy


def restore_backup(bundle, db_path=None, media_root=None, include_media=True, safety_dir=None,
                   force_side_files=False, ignore_migrations=False) -> dict:
    """Replace the database (and write back media files) from a bundle. Everything that can fail is checked first."""
    db_path = Path(db_path or settings.DATABASES["default"]["NAME"])
    media_root = Path(media_root or settings.MEDIA_ROOT)
    incoming = db_path.with_name(db_path.name + ".restoring")
    try:
        manifest = verify_backup(bundle, extract_db_to=incoming, media_root=media_root if include_media else None)
        unknown = [] if ignore_migrations else _unknown_migrations(manifest)
        if unknown:
            raise BackupError(f"This backup records migrations this code does not have ({', '.join(unknown[:3])}): "
                              "usually newer code, so deploy it first. If they were squashed or removed on purpose, "
                              "run again with --ignore-migrations.")
        if str(connection.settings_dict.get("NAME")) == str(db_path):
            connection.close()  # our own handle on the file we are about to replace
        safety_dir = Path(safety_dir) if safety_dir else backup_dir()
        stamp = _stamp()
        _assert_not_in_use(db_path, force_side_files=force_side_files, safety_dir=safety_dir, stamp=stamp)
        safety_copy = _safety_copy(db_path, safety_dir, stamp)
    except BaseException:
        incoming.unlink(missing_ok=True)
        raise

    os.replace(incoming, db_path)  # ---- point of no return: the database is now the backup's
    restored_files, errors = 0, []
    if include_media:
        with tarfile.open(bundle, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.name.startswith(MEDIA_PREFIX):
                    continue
                target = _media_target(media_root, member.name)  # already checked during verify
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with tar.extractfile(member) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    restored_files += 1
                except OSError as exc:
                    errors.append(f"{member.name}: {exc}")
    return {"manifest": manifest, "safety_copy": safety_copy, "media_files": restored_files, "media_errors": errors}


# ---------------------------------------------------------------- listing and pruning
def list_backups(dest_dir=None, kind=None):
    """Bundles oldest first; `kind` is "full", "db" or None for both."""
    dest_dir = Path(dest_dir) if dest_dir else backup_dir()
    if not dest_dir.is_dir():
        return []
    prefixes = [KINDS[kind]] if kind else list(KINDS.values())
    bundles = [p for p in dest_dir.iterdir()
               if p.is_file() and p.name.endswith(BUNDLE_SUFFIX) and p.name.startswith(tuple(prefixes))]
    return sorted(bundles, key=lambda p: (p.stat().st_mtime, p.name))


def prune_backups(keep: int, dest_dir=None, kind="full"):
    """Keep the newest `keep` bundles of one kind; delete older ones of that kind only. Returns the deleted paths."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    old = list_backups(dest_dir, kind)[:-keep]
    for path in old:
        path.unlink()
    return old
