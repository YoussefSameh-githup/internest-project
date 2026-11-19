"""Phase 8: backup bundles (create, verify, restore, prune, safety checks), the backup script, and the hot-query check."""
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings

from . import backups, db_health

SCRIPT = Path(settings.BASE_DIR) / "scripts" / "backup.sh"


def build_bundle(path, members, manifest_files=None, extra_manifest=None):
    """Hand-made bundle: members = [(name, bytes, tar type)]; the manifest lists the regular files (or `manifest_files`)."""
    files = manifest_files if manifest_files is not None else [
        {"name": n, "sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for n, d, t in members if t == tarfile.REGTYPE]
    manifest = {"format": 2, "kind": "full", "created_at": "x", "migrations": {}, "files": files, **(extra_manifest or {})}
    with tarfile.open(path, "w:gz") as tar:
        for name, data, kind in members + [("manifest.json", json.dumps(manifest).encode(), tarfile.REGTYPE)]:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                info.linkname = "/etc/passwd" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
                tar.addfile(info)
    return path


def sqlite_bytes():
    fd, name = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    conn = sqlite3.connect(name)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    data = Path(name).read_bytes()
    os.unlink(name)
    return data


class BackupBase(TransactionTestCase):
    """TransactionTestCase: SQLite's backup API cannot read a database while this connection holds an open write
    transaction (what TestCase wraps every test in); production runs in autocommit."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.media = self.tmp / "media"
        (self.media / "cvs").mkdir(parents=True)
        (self.media / "cvs" / "a.pdf").write_bytes(b"%PDF-1.4 cv")
        (self.media / "partner_logos").mkdir()
        (self.media / "partner_logos" / "l.png").write_bytes(b"\x89PNG logo")
        self.out = self.tmp / "backups"
        self.settings_override = override_settings(MEDIA_ROOT=str(self.media), BACKUP_DIR=str(self.out))
        self.settings_override.enable()
        User.objects.create_user("backed-up", email="b@example.com", password="pw")

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)


class CreateVerifyTests(BackupBase):
    def test_bundle_holds_a_checked_database_and_every_media_file(self):
        bundle = backups.create_backup()
        self.assertTrue(bundle.name.startswith("internest-full-") and bundle.name.endswith(".tar.gz"))
        manifest = backups.verify_backup(bundle)
        self.assertEqual({f["name"] for f in manifest["files"]}, {"db.sqlite3", "media/cvs/a.pdf", "media/partner_logos/l.png"})
        self.assertIn("0029_verified_partner_index", manifest["migrations"]["internest_core"])
        with tarfile.open(bundle) as tar:
            self.assertEqual(tar.getnames()[-1], "manifest.json")  # written last, from the archived bytes
            db = tar.extractfile("db.sqlite3").read()
        self.assertEqual((db[18], db[19]), (1, 1))  # journal_mode=DELETE: never WAL on a network disk
        self.assertFalse(list(self.out.glob("*.partial")))
        if os.name == "posix":
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_database_only_is_its_own_kind(self):
        bundle = backups.create_backup(include_media=False)
        self.assertTrue(bundle.name.startswith("internest-db-"))
        self.assertEqual([f["name"] for f in backups.verify_backup(bundle)["files"]], ["db.sqlite3"])

    def test_corrupt_or_unsafe_bundles_fail(self):
        good = sqlite_bytes()
        reg = tarfile.REGTYPE
        db_entry = {"name": "db.sqlite3", "sha256": hashlib.sha256(good).hexdigest(), "size": len(good)}
        cases = {
            "changed file": ([("db.sqlite3", good, reg), ("media/a.pdf", b"x", reg)],
                             [db_entry, {"name": "media/a.pdf", "sha256": "0" * 64, "size": 1}]),
            "listed ../ path": ([("db.sqlite3", good, reg), ("media/../../evil.py", b"x", reg)], None),
            "listed absolute path": ([("db.sqlite3", good, reg), ("/etc/cron.d/x", b"x", reg)], None),
            "symlink": ([("db.sqlite3", good, reg), ("media/link", b"", tarfile.SYMTYPE)], None),
            "hardlink": ([("db.sqlite3", good, reg), ("media/hard", b"", tarfile.LNKTYPE)], None),
            "directory + file with one name": ([("db.sqlite3", good, reg), ("media/a.pdf", b"", tarfile.DIRTYPE),
                                                ("media/a.pdf", b"x", reg)], None),
            "unlisted member": ([("db.sqlite3", good, reg), ("extra.txt", b"x", reg)], [db_entry]),
            "not a database (checksum ok)": ([("db.sqlite3", b"garbage" * 10, reg)], None),
            "no database": ([("media/a.pdf", b"x", reg)], None),
        }
        for i, (label, (members, files)) in enumerate(cases.items()):
            with self.subTest(label):
                path = build_bundle(self.tmp / f"case{i}.tar.gz", members, files)
                with self.assertRaises(backups.BackupError):
                    backups.verify_backup(path)
        with self.assertRaises(backups.BackupError):
            backups.verify_backup(self.tmp / "missing.tar.gz")
        (self.tmp / "junk.tar.gz").write_bytes(b"not gzip")
        with self.assertRaises(backups.BackupError):
            backups.verify_backup(self.tmp / "junk.tar.gz")

    def test_refuses_unsafe_locations(self):
        for inside in (Path(settings.BASE_DIR) / "backups", self.media / "backups", Path("relative/backups"), Path.home()):
            with self.subTest(inside=str(inside)), self.assertRaises(backups.BackupError):
                backups.create_backup(dest_dir=inside)
        self.assertFalse((Path(settings.BASE_DIR) / "backups").exists())

    def test_prune_keeps_the_newest_of_each_kind(self):
        full = [backups.create_backup() for _ in range(2)]
        db_only = [backups.create_backup(include_media=False) for _ in range(3)]
        for i, path in enumerate(full + db_only):
            os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
        self.assertEqual(backups.prune_backups(1, kind="db"), db_only[:2])
        self.assertEqual(backups.list_backups(kind="full"), full)  # frequent db-only backups never remove full ones
        self.assertEqual(backups.prune_backups(1, kind="full"), full[:1])
        with self.assertRaises(backups.BackupError):
            backups.prune_backups(0)

    def test_a_file_deleted_during_the_backup_is_skipped_not_fatal(self):
        real = Path.read_bytes

        def flaky(path):
            if path.name == "a.pdf":
                raise FileNotFoundError(path)
            return real(path)
        with mock.patch.object(Path, "read_bytes", flaky):
            bundle = backups.create_backup()
        self.assertEqual(backups.verify_backup(bundle)["skipped"], ["cvs/a.pdf"])


class RestoreTests(BackupBase):
    def _target(self):
        target_db = self.tmp / "restored" / "db.sqlite3"
        target_db.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(target_db)
        conn.execute("CREATE TABLE old (x)")
        conn.commit()
        conn.close()
        return target_db

    def test_round_trip_into_a_fresh_location(self):
        bundle = backups.create_backup()
        target_db = self._target()
        target_media = self.tmp / "restored_media"
        (target_media / "cvs").mkdir(parents=True)
        (target_media / "cvs" / "keep.pdf").write_bytes(b"not in the bundle")

        result = backups.restore_backup(bundle, db_path=target_db, media_root=target_media)

        conn = sqlite3.connect(target_db)
        try:
            self.assertEqual(conn.execute("SELECT username FROM auth_user WHERE username='backed-up'").fetchone(), ("backed-up",))
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertEqual(result["safety_copy"].parent, self.out)  # outside the project, owner-only folder
        safety = sqlite3.connect(result["safety_copy"])
        try:
            self.assertEqual(safety.execute("SELECT name FROM sqlite_master").fetchone(), ("old",))
        finally:
            safety.close()
        self.assertEqual((target_media / "cvs" / "a.pdf").read_bytes(), b"%PDF-1.4 cv")
        self.assertEqual((target_media / "cvs" / "keep.pdf").read_bytes(), b"not in the bundle")  # never deleted
        self.assertEqual((result["media_files"], result["media_errors"]), (2, []))
        self.assertFalse(target_db.with_name("db.sqlite3.restoring").exists())

    def test_a_database_in_use_is_refused_and_unchanged(self):
        bundle = backups.create_backup(include_media=False)
        target_db = self._target()
        other = sqlite3.connect(target_db, isolation_level=None)  # another worker, mid-transaction
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO old VALUES (1)")
        try:
            with self.assertRaisesRegex(backups.BackupError, "in use|database open"):  # lock or its journal
                backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m")
        finally:
            other.execute("COMMIT")
            other.close()
        conn = sqlite3.connect(target_db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM old").fetchone(), (1,))  # untouched
        finally:
            conn.close()

    def test_a_bad_bundle_changes_nothing(self):
        bundle = build_bundle(self.tmp / "bad.tar.gz", [("db.sqlite3", b"garbage", tarfile.REGTYPE)])
        target_db = self.tmp / "live.sqlite3"
        target_db.write_bytes(b"current")
        with self.assertRaises(backups.BackupError):
            backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m2")
        self.assertEqual(target_db.read_bytes(), b"current")
        self.assertFalse((self.tmp / "m2").exists())
        self.assertFalse(target_db.with_name("live.sqlite3.restoring").exists())

    def test_a_bundle_from_newer_code_is_refused(self):
        bundle = build_bundle(self.tmp / "newer.tar.gz", [("db.sqlite3", sqlite_bytes(), tarfile.REGTYPE)],
                              extra_manifest={"migrations": {"internest_core": ["9999_from_the_future"]}})
        target_db = self._target()
        with self.assertRaisesMessage(backups.BackupError, "newer code"):
            backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m")

    def test_leftover_side_files_block_unless_forced_and_are_kept(self):
        bundle = backups.create_backup(include_media=False)
        target_db = self._target()
        journal = target_db.with_name("db.sqlite3-journal")
        journal.write_bytes(b"left by a crash")
        with self.assertRaisesMessage(backups.BackupError, "--force-side-files"):
            backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m")
        result = backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m", force_side_files=True)
        self.assertFalse(journal.exists())
        kept = list(self.out.glob("before-restore-*-journal"))
        self.assertEqual([p.read_bytes() for p in kept], [b"left by a crash"])
        self.assertTrue(result["safety_copy"].exists())

    def test_ignore_migrations_override(self):
        bundle = build_bundle(self.tmp / "newer2.tar.gz", [("db.sqlite3", sqlite_bytes(), tarfile.REGTYPE)],
                              extra_manifest={"migrations": {"internest_core": ["9999_squashed_away"]}})
        target_db = self._target()
        backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m", ignore_migrations=True)

    def test_a_large_database_is_not_capped(self):
        big = self.tmp / "big.sqlite3"
        conn = sqlite3.connect(big)
        conn.execute("CREATE TABLE b (x BLOB)")
        conn.executemany("INSERT INTO b VALUES (?)", [(os.urandom(1024 * 1024),) for _ in range(70)])
        conn.commit()
        conn.close()
        data = big.read_bytes()
        bundle = build_bundle(self.tmp / "big.tar.gz", [("db.sqlite3", data, tarfile.REGTYPE)])
        self.assertGreater(len(data), 64 * 1024 * 1024)
        backups.verify_backup(bundle)

    def test_a_corrupt_live_database_is_kept_raw_and_replaced(self):
        bundle = backups.create_backup(include_media=False)
        target_db = self.tmp / "corrupt.sqlite3"
        target_db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)
        result = backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m")
        self.assertEqual(result["safety_copy"].suffix, ".raw")
        self.assertTrue(result["safety_copy"].read_bytes().startswith(b"SQLite format 3"))
        conn = sqlite3.connect(target_db)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()


class CommandTests(BackupBase):
    def test_backup_verify_and_restore_guard(self):
        out = io.StringIO()
        call_command("backup_data", "--keep", "5", stdout=out)
        self.assertIn("Backup OK", out.getvalue())
        out = io.StringIO()
        call_command("verify_backup", stdout=out)
        self.assertIn("database integrity ok", out.getvalue())
        with self.assertRaisesMessage(CommandError, "--yes"):
            call_command("restore_backup", str(backups.list_backups()[-1]))

    def test_verify_with_nothing_to_verify(self):
        with self.assertRaises(CommandError):
            call_command("verify_backup", "--dir", str(self.tmp / "empty"))


@unittest.skipUnless(shutil.which("bash"), "bash not installed")
class ScriptTests(TestCase):
    """Runs the real script with a fake virtualenv whose `python` succeeds or fails."""

    def _run(self, python_body):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "venv" / "bin").mkdir(parents=True)
        (tmp / "venv" / "bin" / "activate").write_bytes(f"python() {{ {python_body}; }}\n".encode())
        env = {**os.environ, "VENV": (tmp / "venv").as_posix(), "BACKUP_LOG_DIR": (tmp / "logs").as_posix(),
               "HOME": tmp.as_posix()}
        proc = subprocess.run(["bash", SCRIPT.as_posix()], env=env, capture_output=True, text=True, timeout=60)
        return proc, (tmp / "logs" / "backup.log").read_text()

    def test_a_failing_step_fails_the_script(self):
        proc, log = self._run('echo "ran $2"; [ "$2" != "backup_data" ] || return 3')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAILED: backup_data", log)
        self.assertNotIn("ran db_health", log)
        self.assertNotIn("backup done", log)

    def test_success_logs_every_step(self):
        proc, log = self._run('echo "ran $2"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for line in ("ran backup_data", "ran db_health", "backup done"):
            self.assertIn(line, log)

    def test_line_endings_stay_lf(self):
        self.assertNotIn(b"\r\n", SCRIPT.read_bytes())


class DbHealthTests(TestCase):
    def test_parser(self):
        plan = ("3 0 216 SCAN internest_core_internship\n4 0 60 SEARCH a USING INDEX b (x=?)\n"
                "5 0 0 SCAN c USING COVERING INDEX d\n6 0 0 USE TEMP B-TREE FOR ORDER BY\n"
                "7 0 0 SCAN (subquery-1)\n8 0 0 SEARCH e USING AUTOMATIC COVERING INDEX (y=?)")
        self.assertEqual(db_health.full_scans(plan), ["internest_core_internship", "e (automatic index)"])

    def test_no_hot_query_scans_a_whole_table(self):
        offenders = {r["name"]: r["full_scans"] for r in db_health.query_plans() if r["full_scans"]}
        self.assertEqual(offenders, {})

    def test_report_and_command(self):
        report = db_health.database_report()
        self.assertEqual(report["quick_check"], "ok")
        self.assertGreater(report["tables"], 20)
        out = io.StringIO()
        call_command("db_health", "--plans", "--optimize", stdout=out)
        self.assertIn("All hot queries use an index.", out.getvalue())
        json.dumps(report)
