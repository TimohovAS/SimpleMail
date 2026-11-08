"""«Очистить корзину» и «Очистить спам» по правой кнопке на папке ящика: всё удаляется навсегда."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from mailclient import mailops, protocols
from mailclient.db import Account, Database, Message
from mailclient.errors import MailOperationError
from mailclient.mailparse import parse_message
from mailclient.protocols import build_message
from mailclient.ui import main_window, viewer

from tests.test_trash_cleanup import SetIMAP


def run_now(fn, *args, local=False, on_done=None, on_error=None, on_progress=None, **kwargs):
    """Очистку и перенос писем выполнить сразу (письма здесь без сервера); проверку почты — нет."""
    if not local and fn not in (mailops.empty_trash, mailops.empty_spam):
        return
    try:
        result = fn(*args, **kwargs)
    except Exception as error:
        if on_error:
            on_error(error)
        return
    if on_done:
        on_done(result)


class TestEmptyTrash(unittest.TestCase):
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
        self.im.boxes["Trash"][20] = b"not downloaded yet"
        for patch in (mock.patch.object(mailops, "imap_connect", return_value=self.im),
                      mock.patch.object(mailops, "get_password", return_value="test")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(protocols.close_pool)
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id, name, uidvalidity, view, server_count) VALUES(?,?,?,?,?)",
                      (self.acc.id, "Trash", 84, "trash", 3))
        m = parse_message(self.raw)
        m.account_id, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "INBOX", 10, 42
        self.mid = self.db.add_message(m)
        mailops.move_to_trash(self.db, self.mid)  # на сервере станет Trash UID 21
        self.im.calls.clear()

    def add_trash(self, uid, **fields):
        m = parse_message(self.raw)
        m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "trash", "Trash", uid, 84
        m.trash_origin_folder = "inbox"
        for key, value in fields.items():
            setattr(m, key, value)
        return self.db.add_message(m)

    def local_trash(self):
        return [r[0] for r in self.db.conn().execute(
            "SELECT id FROM messages WHERE account_id=? AND folder='trash'", (self.acc.id,))]

    def test_count_includes_messages_not_downloaded_yet(self):
        self.assertEqual(mailops.trash_count(self.db, self.acc.id), 3)
        self.add_trash(None, server_folder=None, trash_origin_folder=None)
        for uid in (30, 31):
            self.add_trash(uid)
        self.assertEqual(mailops.trash_count(self.db, self.acc.id), 4)

    def test_whole_server_trash_is_emptied_and_other_folders_untouched(self):
        stale = self.add_trash(7)  # на сервере уже нет: удалили в другой программе
        local = self.add_trash(None, server_folder=None, trash_origin_folder=None)
        self.assertEqual(mailops.empty_trash(self.db, self.acc.id), 4)
        self.assertEqual(self.im.boxes["Trash"], {})
        self.assertEqual(self.local_trash(), [])
        for mid in (self.mid, stale, local):
            self.assertIsNone(self.db.message(mid))
        # Входящие не тронуты, общий EXPUNGE не вызывался (FakeIMAP.expunge падает).
        self.assertEqual(set(self.im.boxes["INBOX"]), {11})
        self.assertEqual(self.im.deleted["INBOX"], {11})
        self.assertEqual(self.im.set_calls, [("STORE", 3), ("EXPUNGE", 3)])
        self.assertFalse(any(call[1] == "INBOX" for call in self.im.calls))
        self.assertEqual(self.db.conn().execute("SELECT server_count FROM folders WHERE name='Trash'").fetchone()[0], 0)
        self.assertEqual(mailops.track_trash(self.db, self.acc.id), {})

    def test_server_without_uidplus_keeps_everything(self):
        self.im.caps = b"IMAP4rev1 MOVE"
        with self.assertRaises(MailOperationError):
            mailops.empty_trash(self.db, self.acc.id)
        self.assertEqual(set(self.im.boxes["Trash"]), {5, 20, 21})
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertFalse(any(call[0] in ("STORE", "EXPUNGE") for call in self.im.calls))

    def test_failed_expunge_keeps_local_copies_and_clears_flag(self):
        self.im.fail_command = "EXPUNGE"
        with self.assertRaises(Exception):
            mailops.empty_trash(self.db, self.acc.id)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertEqual(set(self.im.boxes["Trash"]), {5, 20, 21})
        self.assertNotIn(21, self.im.deleted["Trash"])

    def test_changed_folder_generation_stops_before_deleting(self):
        original = self.im.select

        def select(value):
            result = original(value)
            if self.im.selected == "Trash" and any(call[0] == "SEARCH" for call in self.im.calls):
                self.im.validity["Trash"] = 85  # папку пересоздали между поиском и удалением
            return result

        self.im.select = select
        with self.assertRaises(MailOperationError):
            mailops.empty_trash(self.db, self.acc.id)
        self.assertEqual(set(self.im.boxes["Trash"]), {5, 20, 21})
        self.assertEqual(self.db.message(self.mid).folder, "trash")

    def test_large_trash_is_emptied_in_batches(self):
        for uid in range(100, 551):
            self.im.boxes["Trash"][uid] = b"x"
        ids = [self.add_trash(uid) for uid in range(100, 300)]
        self.assertEqual(mailops.empty_trash(self.db, self.acc.id), 454)
        self.assertEqual(self.im.boxes["Trash"], {})
        self.assertEqual([n for command, n in self.im.set_calls if command == "EXPUNGE"], [200, 200, 54])
        self.assertFalse(any(self.db.message(mid) for mid in ids))

    def test_cancel_keeps_rest_of_trash(self):
        for uid in range(100, 551):
            self.im.boxes["Trash"][uid] = b"x"
        kept = self.add_trash(500)
        cancel = mock.Mock()
        cancel.is_set.side_effect = [False, False, True] + [True] * 10
        self.assertEqual(mailops.empty_trash(self.db, self.acc.id, cancel), 200)
        self.assertEqual(self.db.message(kept).folder, "trash")
        self.assertIn(500, self.im.boxes["Trash"])

    def test_without_server_trash_only_local_copies_are_removed(self):
        with self.db.conn() as c:
            c.execute("DELETE FROM folders")
        self.db.update_message(self.mid, server_folder=None, trash_origin_folder=None)
        with mock.patch.object(mailops, "account_imap") as connect:
            self.assertEqual(mailops.empty_trash(self.db, self.acc.id), 1)
        connect.assert_not_called()
        self.assertIsNone(self.db.message(self.mid))


class TestEmptySpam(unittest.TestCase):
    def setUp(self):
        protocols.close_pool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(email="me@example.com", storage_dir=self.tmp.name)
        self.db.save_account(self.acc)
        self.raw = build_message(self.acc, "spammer@example.com", "Buy", "Body").as_bytes()
        self.im = SetIMAP(self.raw)
        self.im.boxes["Junk"] = {3: b"junk 1", 4: b"junk not downloaded"}
        self.im.deleted["Junk"], self.im.validity["Junk"] = set(), 77
        self.im.listing.append(b'(\\Junk) "/" "Junk"')
        for patch in (mock.patch.object(mailops, "imap_connect", return_value=self.im),
                      mock.patch.object(mailops, "get_password", return_value="test")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(protocols.close_pool)
        with self.db.conn() as c:
            c.executemany("INSERT INTO folders(account_id, name, uidvalidity, view, server_count) VALUES(?,?,?,?,?)",
                          [(self.acc.id, "Trash", 84, "trash", 1), (self.acc.id, "Junk", 77, "spam", 2)])
        m = parse_message(self.raw)
        m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "spam", "Junk", 3, 77
        self.junk = self.db.add_message(m)
        m = parse_message(self.raw)  # спам, определённый программой, остался во «Входящих» на сервере
        m.account_id, m.server_folder, m.uid, m.uidvalidity, m.is_spam = self.acc.id, "INBOX", 10, 42, True
        self.inbox_spam = self.db.add_message(m)
        self.local = self.db.add_message(Message(account_id=self.acc.id, folder="spam", subject="local", date_ts=1))

    def test_count_includes_spam_left_in_inbox_and_not_downloaded(self):
        self.assertEqual(mailops.spam_count(self.db, self.acc.id), 4)

    def test_spam_is_deleted_for_good_and_trash_is_untouched(self):
        self.assertEqual(mailops.empty_spam(self.db, self.acc.id), 4)
        self.assertEqual(self.im.boxes["Junk"], {})
        self.assertEqual(set(self.im.boxes["INBOX"]), {11})  # чужое письмо с \Deleted не тронуто
        self.assertEqual(self.im.boxes["Trash"], {5: b"other trash"})  # спам прошёл через корзину и удалён
        for mid in (self.junk, self.inbox_spam, self.local):
            self.assertIsNone(self.db.message(mid))
        self.assertEqual(mailops.spam_count(self.db, self.acc.id), 0)


class TestTrashMenu(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.account = self.db.save_account(Account(name="Work", email="w@example.com", storage_dir=self.tmp.name))
        self.trash = [self.db.add_message(Message(account_id=self.account, folder="trash", subject=f"T{i}",
                                                  sender="a@example.com", date_ts=1700000000 + i, seen=True))
                      for i in range(2)]
        self.spam = [self.db.add_message(Message(account_id=self.account, folder="inbox", is_spam=True, subject="S",
                                                 sender="s@example.com", date_ts=1700000100, seen=True)),
                     self.db.add_message(Message(account_id=self.account, folder="spam", subject="J",
                                                 sender="s@example.com", date_ts=1700000200, seen=True))]
        self.inbox = self.db.add_message(Message(account_id=self.account, subject="Keep", sender="k@example.com",
                                                 date_ts=1700000300, seen=True))
        for patch in (mock.patch.object(main_window, "run_task", side_effect=run_now),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.addCleanup(self.close_window)
        self.window.show()
        self.app.processEvents()

    def close_window(self):
        self.window.close()
        self.app.processEvents()

    def menu_actions(self, view):
        item = self.window._folder_item(self.account, view)
        self.assertIsNotNone(item)
        pos = self.window.tree.visualItemRect(item).center()
        shown = []

        class Menu(QMenu):
            def exec(self, *args):
                shown.append([(a.text(), a.isEnabled()) for a in self.actions()])

        with mock.patch.object(main_window, "QMenu", Menu):
            self.window._account_menu(pos)
        return shown[0] if shown else None

    def test_trash_and_spam_folders_have_clear_items(self):
        self.assertEqual(self.menu_actions("trash"), [("Очистить корзину", True)])
        self.assertEqual(self.menu_actions("spam"), [("Очистить спам", True)])
        self.assertIsNone(self.menu_actions("inbox"))

    def test_empty_trash_asks_and_deletes_all_messages_of_this_account(self):
        other = self.db.save_account(Account(name="Other", email="o@example.com", storage_dir=self.tmp.name))
        foreign = self.db.add_message(Message(account_id=other, folder="trash", subject="X", date_ts=1))
        with mock.patch.object(main_window.QMessageBox, "question", return_value=QMessageBox.No) as ask:
            self.window._clear_folder(self.account, "trash")
        self.assertIn("Work", ask.call_args.args[2])
        self.assertIn("2", ask.call_args.args[2])
        self.assertTrue(all(self.db.message(mid) for mid in self.trash))
        with mock.patch.object(main_window.QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window._clear_folder(self.account, "trash")
        self.app.processEvents()
        self.assertFalse(any(self.db.message(mid) for mid in self.trash))
        self.assertIsNotNone(self.db.message(foreign))
        self.assertIsNotNone(self.db.message(self.inbox))
        self.assertFalse(self.window._mail_busy)
        self.assertEqual(self.menu_actions("trash"), [("Очистить корзину", False)])

    def test_empty_spam_deletes_spam_without_trash(self):
        with mock.patch.object(main_window.QMessageBox, "question", return_value=QMessageBox.Yes) as ask:
            self.window._clear_folder(self.account, "spam")
        self.app.processEvents()
        self.assertIn("навсегда", ask.call_args.args[2])
        self.assertIn("2", ask.call_args.args[2])
        self.assertFalse(any(self.db.message(mid) for mid in self.spam))
        self.assertTrue(all(self.db.message(mid) for mid in self.trash))  # корзина не тронута
        self.assertEqual(self.db.message(self.inbox).folder, "inbox")
        self.assertEqual(self.menu_actions("spam"), [("Очистить спам", False)])


if __name__ == "__main__":
    unittest.main()
