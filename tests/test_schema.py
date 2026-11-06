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

from mailclient.db import (SCHEMA_VERSION, Account, Database, SchemaMismatch, remove_database,
                           set_aside_database)
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

    def empty_old_database(self, version=0):
        """База другой схемы без ящиков и писем: только настройки и служебные таблицы FTS."""
        Database(self.path).close()
        c = sqlite3.connect(self.path)
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('language', 'ru')")
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

    def test_database_with_mailbox_is_not_empty(self):
        self.old_database(0)
        with self.assertRaises(SchemaMismatch) as caught:
            Database(self.path)
        self.assertFalse(caught.exception.empty)

    def test_database_without_mailboxes_is_reported_empty(self):
        self.empty_old_database(0)
        with self.assertRaises(SchemaMismatch) as caught:
            Database(self.path)
        self.assertTrue(caught.exception.empty)
        self.assertEqual(self.schema(), 0)  # сама программа ничего не меняет

    def test_any_user_row_in_unknown_table_is_data(self):
        c = sqlite3.connect(self.path)
        c.execute('CREATE TABLE "old ""odd"" table"(x)')
        c.execute('INSERT INTO "old ""odd"" table" VALUES(1)')
        c.commit()
        c.close()
        with self.assertRaises(SchemaMismatch) as caught:
            Database(self.path)
        self.assertFalse(caught.exception.empty)

    def test_set_aside_keeps_old_file_and_allows_new_database(self):
        self.empty_old_database(0)
        Path(str(self.path) + "-wal").write_bytes(b"")
        first = set_aside_database(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(first.exists())
        self.assertTrue(Path(str(first) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        Database(self.path).close()
        self.assertEqual(self.schema(), SCHEMA_VERSION)
        second = set_aside_database(self.path)  # то же время — другое имя, ничего не затирается
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_remove_database_deletes_companions(self):
        self.empty_old_database(0)
        Path(str(self.path) + "-wal").write_bytes(b"")
        remove_database(self.path)
        self.assertEqual(list(self.dir.iterdir()), [])
        remove_database(self.path)  # повторно — не ошибка


class TestEmptyOldDatabaseDialog(Base):
    """main.open_other_schema: пустую базу — только по согласию, копия — только по флажку."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def answer(self, error, button, keep=False):
        import main
        from PySide6.QtWidgets import QCheckBox, QMessageBox
        shown = []

        def fake_exec(box):
            shown.append(box.text())
            if keep:
                box.checkBox().setChecked(True)
            [b for b in box.buttons() if b.text() == button][0].click()
            return 0
        with mock.patch.object(QMessageBox, "exec", fake_exec):
            db = main.open_other_schema(error)
        if db is not None:
            self.addCleanup(db.close)
        return db, shown

    def mismatch(self):
        with self.assertRaises(SchemaMismatch) as caught:
            Database(self.path)
        return caught.exception

    def test_exit_leaves_database_untouched(self):
        self.empty_old_database(0)
        db, shown = self.answer(self.mismatch(), "Выход")
        self.assertIsNone(db)
        self.assertEqual(self.schema(), 0)
        self.assertIn("нет ни ящиков, ни писем", shown[0])
        self.assertNotIn("migrate_db", shown[0])

    def test_create_deletes_old_file_by_default(self):
        self.empty_old_database(0)
        db, _ = self.answer(self.mismatch(), "Создать новую базу")
        self.assertIsNotNone(db)
        self.assertEqual(self.schema(), SCHEMA_VERSION)
        self.assertEqual(list(self.dir.glob("mail.db.old-*")), [])

    def test_create_keeps_copy_when_asked(self):
        self.empty_old_database(0)
        db, _ = self.answer(self.mismatch(), "Создать новую базу", keep=True)
        self.assertIsNotNone(db)
        self.assertEqual(self.schema(), SCHEMA_VERSION)
        self.assertEqual(len(list(self.dir.glob("mail.db.old-*"))), 1)

    def test_database_with_data_is_only_reported(self):
        self.old_database(0)
        from PySide6.QtWidgets import QMessageBox
        with mock.patch.object(QMessageBox, "critical") as critical:
            import main
            self.assertIsNone(main.open_other_schema(self.mismatch()))
        text = critical.call_args.args[2]
        self.assertNotIn("migrate_db", text)
        self.assertNotIn("исходным кодом", text)
        self.assertEqual(self.schema(), 0)


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
