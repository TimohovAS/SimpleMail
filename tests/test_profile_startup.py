"""Startup and first-account registration use only temporary local profiles."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QMessageBox

import main as entrypoint
from mailclient import profiles
from mailclient.db import Account, Database, SCHEMA_VERSION
from mailclient.ui import account_dialog, main_window


class WindowStub(QObject):
    closed = Signal()
    profile_changed = Signal()

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.shown = False

    def show(self):
        self.shown = True

    def bring_to_front(self):
        pass


class ApplicationStub(QObject):
    commitDataRequest = Signal(object)

    def __init__(self):
        super().__init__()
        self.during_exec = lambda: None
        self.exec_count = 0

    def setQuitOnLastWindowClosed(self, _enabled):
        pass

    def quit(self):
        pass

    def exec(self):
        self.exec_count += 1
        self.during_exec()
        return 0


class AccountsStub(QObject):
    account_saved = Signal()

    def __init__(self, db):
        super().__init__()
        self.db = db
        self._reload = mock.Mock()


class TestProfileStartup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.marker = self.root / "locator" / "profile.json"
        self.default = self.root / "unused default"
        self.app = ApplicationStub()
        self.window = None
        self.database_calls = []

        def open_database(path):
            self.database_calls.append(Path(path))
            db = Database(path)
            self.addCleanup(db.close)
            return db

        def create_window(db):
            self.window = WindowStub(db)
            return self.window

        patches = {
            "environment": mock.patch.dict(os.environ, {profiles.PROFILE_FILE_ENV: str(self.marker)}),
            "early_language": mock.patch.object(entrypoint, "early_language"),
            "language": mock.patch.object(entrypoint, "startup_language"),
            "entry_default": mock.patch.object(entrypoint, "default_data_dir", return_value=self.default),
            "profile_default": mock.patch.object(profiles, "default_data_dir", return_value=self.default),
            "database": mock.patch.object(entrypoint, "Database", side_effect=open_database),
            "application": mock.patch.object(entrypoint, "create_application", return_value=self.app),
            "window": mock.patch.object(main_window, "MainWindow", side_effect=create_window),
            "lease": mock.patch.object(entrypoint, "_instance_lease", None),
            "installation_lease": mock.patch.object(entrypoint, "_installation_lease", None),
            "acquire": mock.patch("mailclient.instance.acquire_instance", return_value=object()),
            "activation": mock.patch("mailclient.instance.start_activation_server"),
            "installation": mock.patch("mailclient.updates.hold_installation_mutex", return_value=object()),
            "keyring": mock.patch("mailclient.passwords.initialize_keyring"),
            "logging": mock.patch("mailclient.logs.setup_logging"),
            "startup": mock.patch("mailclient.windows_integration.refresh_startup_profile", create=True),
            "pool": mock.patch("PySide6.QtCore.QThreadPool.globalInstance"),
            "critical": mock.patch.object(QMessageBox, "critical"),
            "warning": mock.patch.object(QMessageBox, "warning"),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.mocks["pool"].return_value.waitForDone.return_value = True

    def seed_profile(self, name, *, remember=False):
        db = Database(self.root / name / "mail.db")
        self.addCleanup(db.close)
        db.save_account(Account(name="Disabled test account", enabled=False))
        if remember:
            profiles.remember_profile(db)
        return db

    def start(self, *arguments, exits=True):
        with mock.patch("sys.argv", ["main.py", *map(str, arguments)]):
            if exits:
                with self.assertRaises(SystemExit) as result:
                    entrypoint.main()
                self.assertEqual(result.exception.code, 0)
            else:
                self.assertIsNone(entrypoint.main())

    def test_first_account_is_registered_immediately_after_profile_changed(self):
        folder = self.root / "initially empty"

        def add_first_account():
            self.assertTrue(self.window.shown)
            self.assertEqual(self.window.db.accounts(), [])
            self.assertFalse(self.marker.exists())
            self.mocks["startup"].assert_not_called()
            self.window.db.save_account(Account(name="First account", enabled=False))
            self.assertFalse(self.marker.exists())
            self.window.profile_changed.emit()
            self.assertEqual(profiles.select_data_dir(), folder)
            self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8")),
                             {"version": 1, "data_dir": str(folder)})

        self.app.during_exec = add_first_account
        self.start("--data", folder)
        self.assertEqual(self.database_calls, [folder / "mail.db"])
        self.mocks["warning"].assert_not_called()
        self.mocks["critical"].assert_not_called()
        self.mocks["activation"].return_value.close.assert_called_once()

    def test_isolated_startup_and_account_signal_leave_existing_locator_unchanged(self):
        previous = self.seed_profile("existing", remember=True)
        original = self.marker.read_bytes()
        isolated = self.root / "isolated"

        def add_isolated_account():
            self.assertTrue(self.window.isolated_profile)
            self.assertEqual(self.marker.read_bytes(), original)
            self.window.db.save_account(Account(name="Isolated account", enabled=False))
            self.window.profile_changed.emit()
            self.assertEqual(self.marker.read_bytes(), original)
            self.assertEqual(profiles.select_data_dir(), Path(previous.path).parent)

        self.app.during_exec = add_isolated_account
        with mock.patch.object(entrypoint, "remember_profile", wraps=profiles.remember_profile) as remember:
            self.start("--data", isolated, "--isolated-profile")
        remember.assert_not_called()
        self.mocks["startup"].assert_not_called()
        self.assertEqual(self.marker.read_bytes(), original)

    def test_missing_registered_profile_stops_before_database_and_window(self):
        missing = self.root / "disconnected disk"
        self.marker.parent.mkdir()
        self.marker.write_text(json.dumps({"version": 1, "data_dir": str(missing)}), encoding="utf-8")
        original = self.marker.read_bytes()
        self.start(exits=False)
        self.mocks["database"].assert_not_called()
        self.mocks["window"].assert_not_called()
        self.mocks["acquire"].assert_not_called()
        self.mocks["critical"].assert_called_once()
        self.assertIn(str(missing), self.mocks["critical"].call_args.args[2])
        self.assertFalse(missing.exists())
        self.assertFalse(self.default.exists())
        self.assertEqual(self.marker.read_bytes(), original)
        self.assertEqual(self.app.exec_count, 0)

    def test_other_schema_keeps_previous_locator_and_database(self):
        self.seed_profile("previous", remember=True)
        original = self.marker.read_bytes()
        old = self.seed_profile("old schema")
        old_path = Path(old.path)
        old.conn().execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        old.close()
        with mock.patch.object(entrypoint, "remove_database") as remove, \
                mock.patch.object(entrypoint, "set_aside_database") as aside:
            self.start("--data", old_path.parent, exits=False)
        self.mocks["window"].assert_not_called()
        self.mocks["startup"].assert_not_called()
        self.mocks["critical"].assert_called_once()
        remove.assert_not_called()
        aside.assert_not_called()
        self.assertEqual(self.marker.read_bytes(), original)
        with closing(sqlite3.connect(old_path)) as checked:
            self.assertEqual(checked.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION - 1)
            self.assertEqual(checked.execute("SELECT count(*) FROM accounts").fetchone()[0], 1)

    def test_corrupt_database_shows_error_without_replacing_locator(self):
        self.seed_profile("previous", remember=True)
        original = self.marker.read_bytes()
        corrupt = self.root / "damaged"
        corrupt.mkdir()
        (corrupt / "mail.db").write_bytes(b"damaged database")
        with mock.patch.object(entrypoint, "Database", side_effect=sqlite3.DatabaseError("file is not a database")) as opened:
            self.start("--data", corrupt, exits=False)
        opened.assert_called_once_with(corrupt / "mail.db")
        self.mocks["window"].assert_not_called()
        self.mocks["startup"].assert_not_called()
        self.mocks["critical"].assert_called_once()
        self.assertEqual(self.marker.read_bytes(), original)
        self.assertEqual((corrupt / "mail.db").read_bytes(), b"damaged database")

    def test_accounts_dialog_add_emits_saved_after_accepted_edit(self):
        db = Database(self.root / "accounts dialog" / "mail.db")
        self.addCleanup(db.close)
        dialog = AccountsStub(db)
        saved = []
        dialog.account_saved.connect(lambda: saved.append(db.accounts()[0].name))

        def accept_account():
            db.save_account(Account(name="Accepted account", enabled=False))
            return QDialog.Accepted

        with mock.patch.object(account_dialog, "AccountEditDialog") as edit:
            edit.return_value.exec.side_effect = accept_account
            account_dialog.AccountsDialog._add(dialog)
        self.assertEqual(saved, ["Accepted account"])
        dialog._reload.assert_called_once()

    def test_accounts_dialog_cancel_does_not_emit_saved(self):
        dialog = AccountsStub(None)
        saved = mock.Mock()
        dialog.account_saved.connect(saved)
        with mock.patch.object(account_dialog, "AccountEditDialog") as edit:
            edit.return_value.exec.return_value = QDialog.Rejected
            account_dialog.AccountsDialog._add(dialog)
        saved.assert_not_called()
        dialog._reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
