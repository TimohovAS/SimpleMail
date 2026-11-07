"""Папки Gmail: дубли «Вся почта»/«Важное»/«Помеченные» не загружаются, префикс [Gmail]/ скрыт."""
import os
import unittest
from unittest import mock
from tests.ui_tasks import run_local

from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient import protocols, sync
from mailclient.mailparse import parse_message
from test_complete_features import FolderIMAP
from test_grok_fixes import Fixture

GMAIL_LIST = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"',
    b'(\\HasNoChildren \\Important) "/" "[Gmail]/Important"',
    b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
    b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
    b'(\\Flagged \\HasNoChildren) "/" "[Gmail]/Starred"',
    b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
    b'(\\HasNoChildren) "/" "Work"',
]


class TestGmailFolders(Fixture):
    def test_duplicate_labels_are_skipped_and_names_are_clean(self):
        im = mock.Mock(utf8_enabled=False)
        im.list.return_value = ("OK", GMAIL_LIST)
        folders = {folder.name: folder for folder in sync.discover_folders(im)}
        self.assertEqual({name for name, f in folders.items() if f.view == "skip"},
                         {"[Gmail]/All Mail", "[Gmail]/Important", "[Gmail]/Starred"})
        self.assertEqual(folders["[Gmail]/Sent Mail"].view, "sent")
        self.assertEqual(folders["[Gmail]/Spam"].view, "spam")
        self.assertEqual(folders["[Gmail]/Trash"].view, "trash")
        self.assertEqual(folders["[Gmail]/Drafts"].display, "Черновики на сервере")
        self.assertEqual(sync.folder_base("[Gmail]/Черновики", "/"), "Черновики")
        self.assertEqual(folders["Work"].view, "remote:Work")

    def test_copies_from_skipped_labels_are_removed_without_touching_inbox(self):
        raw = protocols.build_message(self.acc, "b@example.com", "Hello", "body").as_bytes()

        class Gmail(FolderIMAP):
            def list(self, *args):
                return "OK", [b'() "/" "INBOX"', b'(\\All) "/" "[Gmail]/All Mail"']
        fake = Gmail({"INBOX": {1: (raw, "")}, "[Gmail]/All Mail": {1: (raw, "")}})
        copy = parse_message(raw)
        copy.account_id, copy.folder, copy.server_folder, copy.uid = self.acc.id, "remote:[Gmail]/All Mail", "[Gmail]/All Mail", 1
        self.db.add_message(copy)
        self.db.set_setting(f"history_skipped:{self.acc.id}:[Gmail]/All Mail", "500")
        with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc)
        rows = self.db.conn().execute("SELECT folder,server_folder FROM messages").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("inbox", "INBOX")])
        self.assertIsNone(self.db.get_setting(f"history_skipped:{self.acc.id}:[Gmail]/All Mail"))
        self.assertFalse(any(row["name"] == "[Gmail]/All Mail" for row in self.db.server_folders(self.acc.id)))


class TestSizeBatches(unittest.TestCase):
    def test_large_message_goes_alone_and_small_ones_are_grouped(self):
        im = mock.Mock()
        sizes = {1: 1000, 2: 1000, 3: 5 * 1024 * 1024, 4: 1000, 5: 1000}
        im.uid.return_value = ("OK", [f"{n} (UID {uid} RFC822.SIZE {size})".encode()
                                      for n, (uid, size) in enumerate(sizes.items(), 1)])
        self.assertEqual(sync._size_batches(im, [1, 2, 3, 4, 5]), [[1, 2], [3], [4, 5]])
        im.uid.return_value = ("NO", [b"unsupported"])
        self.assertEqual(sync._size_batches(im, list(range(1, 46))),
                         [list(range(1, 21)), list(range(21, 41)), list(range(41, 46))])


class TestHistoryQuestionOnce(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_second_finished_check_does_not_open_second_question(self):
        from mailclient.ui import main_window
        self.db.set_setting(f"history_skipped:{self.acc.id}:INBOX", "100")
        with mock.patch.object(main_window, "run_task", side_effect=run_local), mock.patch("mailclient.ui.mail_watch.MailWatcher"):
            window = main_window.MainWindow(self.db)
            self.addCleanup(window.close)
            shown = []

            def nested_exec(box):
                shown.append(box)
                window._offer_history()  # вторая проверка закончилась, пока окно открыто
                return 0
            with mock.patch.object(QMessageBox, "exec", nested_exec):
                window._offer_history()
        self.assertEqual(len(shown), 1)
        # «Не сейчас» без галочки: до перезапуска программы больше не спрашиваем.
        self.assertIsNone(self.db.get_setting(f"history:{self.acc.id}"))
        with mock.patch.object(QMessageBox, "exec", nested_exec):
            window._offer_history()
        self.assertEqual(len(shown), 1)
        top = window.tree.topLevelItem(0)
        self.assertTrue(top.font(0).bold())

    def test_never_ask_again(self):
        from mailclient.ui import main_window
        self.db.set_setting(f"history_skipped:{self.acc.id}:INBOX", "100")
        with mock.patch.object(main_window, "run_task", side_effect=run_local), mock.patch("mailclient.ui.mail_watch.MailWatcher"):
            window = main_window.MainWindow(self.db)
            self.addCleanup(window.close)
            shown = []

            def tick_and_close(box):
                shown.append(box)
                box.checkBox().setChecked(True)
                return 0
            with mock.patch.object(QMessageBox, "exec", tick_and_close):
                window._offer_history()
                window._history_not_now.clear()  # как после перезапуска
                window._offer_history()
        self.assertEqual(len(shown), 1)
        self.assertEqual(self.db.get_setting(f"history:{self.acc.id}"), "declined")
        self.assertEqual(self.db.get_setting("history_ask"), "0")


class TestHistoryProgress(Fixture):
    def test_downloaded_and_total_for_menu(self):
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,uidvalidity) VALUES(?,?,?,?)",
                      (self.acc.id, "INBOX", "inbox", 7))
            c.execute("INSERT INTO folders(account_id,name,view) VALUES(?,?,?)",
                      (self.acc.id, "[Gmail]/All Mail", "all"))  # пропускаемая папка: без server_count
        self.assertEqual(self.db.history_progress(self.acc.id), (0, 0))
        self.db.set_server_count(self.acc.id, "INBOX", 10)
        with self.db.conn() as c:
            for uid in (1, 2, 3):
                c.execute("INSERT INTO messages(account_id,folder,server_folder,uid,uidvalidity,message_id,date_ts) "
                          "VALUES(?,?,?,?,?,?,0)", (self.acc.id, "inbox", "INBOX", uid, 7, f"<{uid}@x>"))
        self.assertEqual(self.db.history_progress(self.acc.id), (3, 10))


if __name__ == "__main__":
    unittest.main()
