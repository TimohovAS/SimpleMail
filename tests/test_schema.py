"""Номер схемы базы: программа открывает только базу своей схемы, обновляет её tools/migrate_db.py."""
import contextlib
import importlib.util
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailclient.db import SCHEMA_VERSION, Account, Database, SchemaMismatch
from mailclient.instance import acquire_instance

ROOT = Path(__file__).resolve().parents[1]


def load_migrate_db():
    spec = importlib.util.spec_from_file_location("migrate_db", ROOT / "tools" / "migrate_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "mail.db"

    def schema(self):
        c = sqlite3.connect(self.path)
        try:
            return c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            c.close()

    def old_database(self, version=SCHEMA_VERSION - 1):
        """База другой схемы: таблицы есть, номер схемы не тот."""
        db = Database(self.path)
        db.save_account(Account(name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s"))
        db.close()
        c = sqlite3.connect(self.path)
        c.execute(f"PRAGMA user_version = {version}")
        c.commit()
        c.close()


class TestSchemaVersion(Base):
    def test_new_database_gets_current_schema(self):
        Database(self.path).close()
        self.assertEqual(self.schema(), SCHEMA_VERSION)
        Database(self.path).close()  # своя схема открывается снова

    def test_other_schema_is_refused_and_left_untouched(self):
        self.old_database(0)
        c = sqlite3.connect(self.path)
        before = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        c.close()
        with self.assertRaises(SchemaMismatch) as caught:
            Database(self.path)
        self.assertEqual((caught.exception.found, caught.exception.expected), (0, SCHEMA_VERSION))
        c = sqlite3.connect(self.path)
        self.assertEqual(c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(), before)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 0)
        c.close()


class TestMigrateScript(Base):
    def run_script(self, *args):
        module = load_migrate_db()
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["migrate_db.py", *args, str(self.path)]), contextlib.redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    def backups(self):
        return sorted(p.name for p in self.dir.glob("mail.db.before-*"))

    def test_current_schema_needs_nothing(self):
        Database(self.path).close()
        code, out = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("менять нечего", out)
        self.assertEqual(self.backups(), [])

    def test_unknown_schema_is_not_touched(self):
        self.old_database(SCHEMA_VERSION + 1)
        code, out = self.run_script()
        self.assertEqual(code, 4)
        self.assertIn("такого перехода нет", out)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.schema(), SCHEMA_VERSION + 1)

    def test_refuses_while_simplemail_runs(self):
        self.old_database(0)
        lease = acquire_instance(self.dir)
        self.assertIsNotNone(lease)
        self.addCleanup(getattr(lease, "close", lambda: None))
        code, out = self.run_script()
        self.assertEqual(code, 3)
        self.assertIn("SimpleMail запущен", out)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.schema(), 0)

    def test_missing_database(self):
        code, out = self.run_script()
        self.assertEqual(code, 2)
        self.assertIn("Нет базы", out)


if __name__ == "__main__":
    unittest.main()
