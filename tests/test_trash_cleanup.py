"""Автоочистка корзины: срок из настроек ящика, только свои UID, схема базы не меняется."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import mailops, protocols
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message
from mailclient.protocols import build_message

from tests.test_mailops import FakeIMAP

DAY = 86400
T0 = 1_800_000_000


class SetIMAP(FakeIMAP):
    """FakeIMAP, понимающий наборы UID через запятую в STORE и UID EXPUNGE."""

    def uid(self, command, *args):
        if command in ("STORE", "EXPUNGE") and "," in str(args[0]):
            self.set_calls = getattr(self, "set_calls", []) + [(command, len(str(args[0]).split(",")))]
            result = None
            for uid in str(args[0]).split(","):
                result = super().uid(command, uid, *args[1:])
                if result[0] != "OK":
                    return result
            return result
        return super().uid(command, *args)


class TestTrashCleanup(unittest.TestCase):
    def setUp(self):
        protocols.close_pool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(email="me@example.com", storage_dir=self.tmp.name)
        self.db.save_account(self.acc)
        self.raw = build_message(self.acc, "other@example.com", "Subject", "Body").as_bytes()
        self.im = SetIMAP(self.raw)
        self.im.boxes["Trash"][20] = b"second"
        patch = mock.patch.object(mailops, "imap_connect", return_value=self.im)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(protocols.close_pool)
        patch = mock.patch.object(mailops, "get_password", return_value="test")
        patch.start()
        self.addCleanup(patch.stop)
        m = parse_message(self.raw)
        m.account_id, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "INBOX", 10, 42
        self.mid = self.db.add_message(m)
        mailops.move_to_trash(self.db, self.mid)  # на сервере станет Trash UID 21
        self.trash_uid = self.db.message(self.mid).uid

    def add_trash(self, uid, **fields):
        m = parse_message(self.raw)
        m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "trash", "Trash", uid, 84
        m.trash_origin_folder = "inbox"
        for key, value in fields.items():
            setattr(m, key, value)
        return self.db.add_message(m)

    def purge(self, now):
        return mailops.purge_old_trash(self.db, self.acc, self.im, now=now)

    def test_disabled_by_default_but_time_in_trash_is_tracked(self):
        self.assertEqual(mailops.trash_days(self.db, self.acc.id), 0)
        self.assertEqual(self.purge(T0), 0)
        self.assertEqual(self.purge(T0 + 1000 * DAY), 0)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertEqual(mailops.track_trash(self.db, self.acc.id, T0 + 2000 * DAY), {self.mid: T0})

    def test_deletes_only_messages_older_than_limit_and_only_their_uids(self):
        mailops.set_trash_days(self.db, self.acc.id, 30)
        self.purge(T0)
        newer = self.add_trash(20)
        self.purge(T0 + 10 * DAY)
        self.assertEqual(self.purge(T0 + 30 * DAY - 1), 0)
        self.assertEqual(self.purge(T0 + 30 * DAY), 1)
        self.assertIsNone(self.db.message(self.mid))
        self.assertIsNotNone(self.db.message(newer))
        self.assertNotIn(self.trash_uid, self.im.boxes["Trash"])
        # Чужое письмо с \Deleted (UID 5) и более новое письмо (UID 20) не тронуты.
        self.assertEqual(set(self.im.boxes["Trash"]), {5, 20})
        self.assertEqual(self.im.deleted["Trash"], {5})
        self.assertIn(("EXPUNGE", "Trash", (str(self.trash_uid),)), self.im.calls)
        self.assertEqual(self.purge(T0 + 40 * DAY), 1)
        self.assertEqual(set(self.im.boxes["Trash"]), {5})
        self.assertEqual(mailops.track_trash(self.db, self.acc.id, T0 + 41 * DAY), {})

    def test_restored_message_starts_counting_again(self):
        mailops.set_trash_days(self.db, self.acc.id, 7)
        self.purge(T0)
        mailops.restore_message(self.db, self.mid)
        self.purge(T0 + 3 * DAY)
        mailops.move_to_trash(self.db, self.mid)
        self.purge(T0 + 5 * DAY)
        self.assertEqual(self.purge(T0 + 8 * DAY), 0)
        self.assertEqual(self.purge(T0 + 12 * DAY), 1)

    def test_server_without_uidplus_or_changed_validity_keeps_messages(self):
        mailops.set_trash_days(self.db, self.acc.id, 1)
        self.purge(T0)
        self.im.caps = b"IMAP4rev1 MOVE"
        self.assertEqual(self.purge(T0 + 2 * DAY), 0)
        self.im.caps = b"IMAP4rev1 MOVE UIDPLUS"
        self.im.validity["Trash"] = 85
        self.assertEqual(self.purge(T0 + 2 * DAY), 0)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertIn(self.trash_uid, self.im.boxes["Trash"])
        self.assertFalse(any(call[0] in ("STORE", "EXPUNGE") for call in self.im.calls))

    def test_failed_expunge_clears_deleted_flag_and_keeps_local_copy(self):
        mailops.set_trash_days(self.db, self.acc.id, 1)
        self.purge(T0)
        self.im.fail_command = "EXPUNGE"
        with self.assertRaises(Exception):
            self.purge(T0 + 2 * DAY)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertNotIn(self.trash_uid, self.im.deleted["Trash"])

    def test_local_only_trash_copy_is_deleted_without_server(self):
        local = self.add_trash(None, server_folder=None, trash_origin_folder=None)
        mailops.set_trash_days(self.db, self.acc.id, 1)
        self.purge(T0)
        self.im.calls.clear()
        self.im.caps = b"IMAP4rev1"
        self.purge(T0 + 2 * DAY)
        self.assertIsNone(self.db.message(local))
        self.assertFalse(any(call[0] in ("STORE", "EXPUNGE") for call in self.im.calls))

    def test_many_messages_are_deleted_in_batches(self):
        ids = [self.add_trash(uid) for uid in range(100, 551)]
        for uid in range(100, 551):
            self.im.boxes["Trash"][uid] = b"x"
        mailops.set_trash_days(self.db, self.acc.id, 1)
        self.purge(T0)
        self.assertEqual(self.purge(T0 + 2 * DAY), len(ids) + 1)
        self.assertEqual(set(self.im.boxes["Trash"]), {5, 20})
        self.assertEqual([n for command, n in self.im.set_calls if command == "EXPUNGE"], [200, 200, 52])

    def test_settings_are_clamped_and_removed_with_account(self):
        mailops.set_trash_days(self.db, self.acc.id, -5)
        self.assertEqual(mailops.trash_days(self.db, self.acc.id), 0)
        mailops.set_trash_days(self.db, self.acc.id, 10 ** 6)
        self.assertEqual(mailops.trash_days(self.db, self.acc.id), mailops.TRASH_DAYS_MAX)
        self.db.set_setting(f"trash_days:{self.acc.id}", "abc")
        self.assertEqual(mailops.trash_days(self.db, self.acc.id), 0)
        self.db.set_setting(f"trash_since:{self.acc.id}", "[broken")
        self.assertEqual(mailops.track_trash(self.db, self.acc.id, T0), {self.mid: T0})
        self.db.delete_account(self.acc.id)
        self.assertIsNone(self.db.get_setting(f"trash_days:{self.acc.id}"))
        self.assertIsNone(self.db.get_setting(f"trash_since:{self.acc.id}"))


class TestTrashSetting(unittest.TestCase):
    def test_account_dialog_shows_and_saves_days(self):
        from PySide6.QtWidgets import QApplication
        from mailclient.ui.account_dialog import AccountEditDialog
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "mail.db")
            try:
                acc = Account(name="Test", email="me@example.com", username="me", imap_host="imap.example.com",
                              smtp_host="smtp.example.com", storage_dir=tmp)
                db.save_account(acc)
                mailops.set_trash_days(db, acc.id, 14)
                with mock.patch("mailclient.ui.account_dialog.set_password", return_value=True):
                    dialog = AccountEditDialog(db, acc)
                    self.assertEqual(dialog.trash_days.value(), 14)
                    self.assertEqual(dialog.trash_days.specialValueText(), "никогда")
                    dialog.trash_days.setValue(0)
                    dialog._save()
                self.assertEqual(mailops.trash_days(db, acc.id), 0)
                dialog.deleteLater()
                app.processEvents()
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
