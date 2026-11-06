"""Массовые пометки не тормозят программу: одна задача и пачка UID STORE вместо задачи на письмо;
поиск идёт в своём пуле потоков и не ждёт сетевые задачи."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import protocols
from mailclient.db import Account, Database, Message
from mailclient.errors import MailOperationError
from test_core import make_raw
from test_flagged import FlagIMAP


class BulkFlagsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.account(self.db.save_account(Account(
            name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s")))
        self.fake = FlagIMAP({uid: make_raw("shop@aliexpress.com", f"Заказ {uid}", str(uid)) for uid in range(1, 6)}, {})
        self.connect = mock.patch.object(protocols, "imap_connect", return_value=self.fake)
        self.connect_mock = self.connect.start()
        self.addCleanup(self.connect.stop)
        password = mock.patch.object(protocols, "get_password", return_value="x")
        password.start()
        self.addCleanup(password.stop)
        protocols.close_pool()
        self.addCleanup(protocols.close_pool)
        protocols.sync_account(self.db, self.acc)
        self.fake.stores.clear()
        self.fake.store_commands = []
        self.ids = [row["id"] for row in self.db.conn().execute("SELECT id FROM messages ORDER BY uid")]

    def mark_unsent(self, ids, seen=1):
        for mid in ids:
            self.db.update_message(mid, seen=seen, pending_seen=seen)

    def test_one_store_command_and_one_connection_for_many_messages(self):
        self.mark_unsent(self.ids)
        self.connect_mock.reset_mock()
        protocols.set_server_flags(self.db, self.ids, "\\Seen", True, queued=True)
        self.assertEqual(self.fake.store_commands, [("1,2,3,4,5", "+FLAGS.SILENT", "(\\Seen)")])
        self.assertEqual(self.connect_mock.call_count, 1)
        self.assertTrue(all(self.db.message(mid).pending_seen is None for mid in self.ids))
        self.assertEqual({uid for uid, _, _ in self.fake.stores}, {1, 2, 3, 4, 5})

    def test_large_selection_is_split_into_batches(self):
        self.mark_unsent(self.ids)
        with mock.patch.object(protocols, "STORE_BATCH", 2):
            protocols.set_server_flags(self.db, self.ids, "\\Seen", True, queued=True)
        self.assertEqual([args[0] for args in self.fake.store_commands], ["1,2", "3,4", "5"])

    def test_queued_sends_latest_intent_and_skips_already_sent(self):
        self.mark_unsent(self.ids[:2], seen=1)
        self.mark_unsent(self.ids[2:3], seen=0)  # пользователь уже вернул «непрочитанное»
        protocols.set_server_flags(self.db, self.ids, "\\Seen", True, queued=True)  # у 4 и 5 намерения нет
        self.assertEqual(sorted(self.fake.store_commands),
                         [("1,2", "+FLAGS.SILENT", "(\\Seen)"), ("3", "-FLAGS.SILENT", "(\\Seen)")])

    def test_changed_folder_keeps_pending_for_next_check(self):
        self.mark_unsent(self.ids)
        with mock.patch.object(type(self.fake), "response", lambda im, code: (code, [b"43"])):
            with self.assertRaises(MailOperationError):
                protocols.set_server_flags(self.db, self.ids, "\\Seen", True, queued=True)
        self.assertEqual(self.fake.store_commands, [])
        self.assertTrue(all(self.db.message(mid).pending_seen == 1 for mid in self.ids))

    def test_local_message_only_clears_pending(self):
        mid = self.db.add_message(Message(account_id=self.acc.id, folder="archive", subject="Локальное",
                                          sender="a@b.c", seen=True, pending_seen=1))
        protocols.set_server_flags(self.db, [mid], "\\Seen", True, queued=True)
        self.assertEqual(self.fake.store_commands, [])
        self.assertIsNone(self.db.message(mid).pending_seen)

    def test_check_pushes_offline_marks_in_one_command(self):
        self.mark_unsent(self.ids)
        protocols.sync_account(self.db, self.acc)
        self.assertEqual([args for args in self.fake.store_commands if args[2] == "(\\Seen)"],
                         [("1,2,3,4,5", "+FLAGS.SILENT", "(\\Seen)")])
        self.assertTrue(all(self.db.message(mid).pending_seen is None for mid in self.ids))


class SearchCancelCase(unittest.TestCase):
    def test_cancelled_search_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            try:
                acc = db.save_account(Account(name="T", email="me@example.com", imap_host="i", smtp_host="s"))
                for i in range(50):
                    db.add_message(Message(account_id=acc, subject=f"Тема {i}", sender="a@b.c", date_ts=i))
                cancel = threading.Event()
                cancel.set()
                self.assertEqual(db.search_messages("тема", acc, "inbox", cancel=cancel), [])
                self.assertEqual(len(db.search_messages("тема", acc, "inbox", cancel=threading.Event())), 50)
            finally:
                db.close()


class LocalPoolCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_local_task_runs_while_network_tasks_hold_every_thread(self):
        from PySide6.QtCore import QThreadPool
        from mailclient.ui import worker
        release = threading.Event()
        pool = QThreadPool.globalInstance()
        _busy = [worker.run_task(release.wait, 30) for _ in range(max(worker.MIN_THREADS, pool.maxThreadCount()) + 2)]
        self.addCleanup(lambda: (release.set(), pool.waitForDone(5000)))
        ran = threading.Event()
        worker.run_task(ran.set, local=True)
        self.assertTrue(ran.wait(5), "поиск ждал сетевые задачи")


class BulkMarkUICase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mailclient.ui import main_window, viewer
        self.main_window = main_window
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        aid = self.db.save_account(Account(name="Work", email="w@example.com", imap_host="i", smtp_host="s"))
        self.ids = [self.db.add_message(Message(account_id=aid, subject=f"M{i}", sender="shop@aliexpress.com",
                                                date_ts=1700000000 + i, seen=False)) for i in range(4)]
        for patch in (mock.patch.object(main_window, "run_task"),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(lambda: (self.window.close(), self.app.processEvents()))

    def server_calls(self, fn):
        return [call for call in self.main_window.run_task.call_args_list if call.args and call.args[0] == fn]

    def test_mark_read_selection_starts_one_task(self):
        self.window.table.selectAll()
        self.main_window.run_task.reset_mock()
        self.window.acts["read"].trigger()
        calls = self.server_calls(protocols.set_server_flags)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(calls[0].args[2]), sorted(self.ids))
        self.assertEqual(calls[0].args[3:], ("\\Seen", True))
        self.assertFalse(self.server_calls(protocols.set_server_flag))
        self.assertTrue(all(self.db.message(mid).seen for mid in self.ids))

    def test_search_runs_in_local_pool(self):
        self.window.search.setText("M1")
        self.window._run_search()
        call = self.server_calls(self.db.search_messages)[-1]
        self.assertTrue(call.kwargs.get("local"))


if __name__ == "__main__":
    unittest.main()
