"""Phase 8: backup bundles (create, verify, restore, prune, safety checks) and the hot-query index check."""
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase, override_settings

from . import backups, db_health

SCRIPT = Path(settings.BASE_DIR) / "scripts" / "backup.sh"


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

    def _rewrite(self, bundle, changes):
        """Copy a bundle, replacing or adding members: {name: bytes}. Returns the new path."""
        target = bundle.with_name("tampered" + backups.BUNDLE_SUFFIX)
        with tarfile.open(bundle, "r:gz") as src, tarfile.open(target, "w:gz") as dst:
            for member in src.getmembers():
                data = changes.pop(member.name, None)
                if data is None:
                    dst.addfile(member, src.extractfile(member))
                else:
                    member.size = len(data)
                    dst.addfile(member, io.BytesIO(data))
            for name, data in changes.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                dst.addfile(info, io.BytesIO(data))
        return target


class CreateVerifyTests(BackupBase):
    def test_bundle_holds_a_checked_database_and_every_media_file(self):
        bundle = backups.create_backup()
        self.assertTrue(bundle.name.startswith("internest-") and bundle.name.endswith(".tar.gz"))
        manifest = backups.verify_backup(bundle)
        names = {f["name"] for f in manifest["files"]}
        self.assertEqual(names, {"db.sqlite3", "media/cvs/a.pdf", "media/partner_logos/l.png"})
        self.assertIn("internest_core", manifest["migrations"])
        self.assertFalse(list(self.out.glob("*.partial")))
        if os.name == "posix":
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_database_only(self):
        manifest = backups.verify_backup(backups.create_backup(include_media=False))
        self.assertEqual([f["name"] for f in manifest["files"]], ["db.sqlite3"])

    def test_tampered_or_unsafe_bundles_fail(self):
        bundle = backups.create_backup()
        for changes in ({"media/cvs/a.pdf": b"changed"}, {"../../evil.py": b"x"}, {"media/../../evil.py": b"x"},
                        {"extra.txt": b"not listed"}, {"db.sqlite3": b"not a database"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(backups.BackupError):
                    backups.verify_backup(self._rewrite(bundle, dict(changes)))

    def test_refuses_to_write_inside_the_project_or_media(self):
        for inside in (Path(settings.BASE_DIR) / "backups", self.media / "backups"):
            with self.subTest(inside=str(inside)), self.assertRaises(backups.BackupError):
                backups.create_backup(dest_dir=inside)
        self.assertFalse((Path(settings.BASE_DIR) / "backups").exists())

    def test_prune_keeps_the_newest(self):
        made = [backups.create_backup(include_media=False) for _ in range(3)]
        for i, path in enumerate(made):
            os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))
        removed = backups.prune_backups(2)
        self.assertEqual(removed, made[:1])
        self.assertEqual(backups.list_backups(), made[1:])
        with self.assertRaises(backups.BackupError):
            backups.prune_backups(0)


class RestoreTests(BackupBase):
    def test_round_trip_into_a_fresh_location(self):
        bundle = backups.create_backup()
        target_db = self.tmp / "restored" / "db.sqlite3"
        target_db.parent.mkdir()
        sqlite3.connect(target_db).close()
        (target_db.parent / "db.sqlite3-wal").write_bytes(b"stale wal from the old database")
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
        self.assertFalse((target_db.parent / "db.sqlite3-wal").exists())  # would corrupt the restored file
        self.assertTrue(result["safety_copy"].exists())
        self.assertEqual((target_media / "cvs" / "a.pdf").read_bytes(), b"%PDF-1.4 cv")
        self.assertEqual((target_media / "cvs" / "keep.pdf").read_bytes(), b"not in the bundle")  # never deleted
        self.assertEqual(result["media_files"], 2)

    def test_a_bad_bundle_changes_nothing(self):
        bundle = self._rewrite(backups.create_backup(), {"media/cvs/a.pdf": b"changed"})
        target_db = self.tmp / "live.sqlite3"
        target_db.write_bytes(b"current")
        with self.assertRaises(backups.BackupError):
            backups.restore_backup(bundle, db_path=target_db, media_root=self.tmp / "m2")
        self.assertEqual(target_db.read_bytes(), b"current")
        self.assertFalse((self.tmp / "m2").exists())


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

    def test_script_is_strict_and_uses_the_commands(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for needle in ("set -euo pipefail", "umask 077", "backup_data --keep", "db_health", "exit 1"):
            self.assertIn(needle, text)


class DbHealthTests(TestCase):
    def test_parser_flags_only_full_scans(self):
        plan = ("3 0 216 SCAN internest_core_internship\n4 0 60 SEARCH a USING INDEX b (x=?)\n"
                "5 0 0 SCAN c USING COVERING INDEX d\n6 0 0 USE TEMP B-TREE FOR ORDER BY")
        self.assertEqual(db_health.full_scans(plan), ["internest_core_internship"])

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
        json.dumps(report)  # printable / loggable
