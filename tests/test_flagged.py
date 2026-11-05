"""Флажок «Важное»: IMAP \\Flagged синхронизируется в обе стороны."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import protocols
from mailclient.db import Account, Database, Message
from test_core import FakeIMAP, make_raw


class FlagIMAP(FakeIMAP):
    def __init__(self, messages, flags):
        super().__init__(messages)
        self.flags = flags   # {uid: set}
        self.stores = []

    def uid(self, cmd, *args):
        if cmd == "FETCH":
            uids = sorted(self.messages) if args[0] == "1:*" else list(map(int, args[0].split(",")))
            data = []
            for u in uids:
                flags = " ".join(sorted(self.flags.get(u, ())))
                meta = f"{u} (UID {u} FLAGS ({flags})".encode()
                if "BODY.PEEK[]" in args[1]:
                    data += [(meta + b" BODY[] {1}", self.messages[u]), b")"]
                else:
                    data.append(meta + b")")
            return "OK", data
        if cmd == "STORE":
            uid, mode, flag = int(args[0]), args[1], args[2].strip("()")
            self.stores.append((uid, mode, flag))
            (self.flags.setdefault(uid, set()).add if mode.startswith("+") else self.flags.setdefault(uid, set()).discard)(flag)
            return "OK", [b""]
        return super().uid(cmd, *args)


class TestFlaggedSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.acc = self.db.account(self.db.save_account(Account(
            name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s")))
        self.fake = FlagIMAP({1: make_raw("a@x.ru", "Один", "1"), 2: make_raw("b@x.ru", "Два", "2")},
                             {1: {"\\Flagged"}})

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def sync(self):
        with mock.patch.object(protocols, "imap_connect", return_value=self.fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            protocols.sync_account(self.db, self.acc)

    def flagged(self):
        return {r["uid"]: r["flagged"] for r in self.db.conn().execute("SELECT uid, flagged FROM messages")}

    def test_server_flags_arrive_and_change(self):
        self.sync()
        self.assertEqual(self.flagged(), {1: 1, 2: 0})
        self.fake.flags = {2: {"\\Flagged"}}   # другой клиент (телефон) переставил флажки
        self.sync()
        self.assertEqual(self.flagged(), {1: 0, 2: 1})

    def test_local_flag_is_pushed_when_server_was_unreachable(self):
        self.sync()
        mid = self.db.conn().execute("SELECT id FROM messages WHERE uid=2").fetchone()[0]
        self.db.update_message(mid, flagged=1, pending_flagged=1)  # set_server_flag не смог подключиться
        self.sync()
        self.assertIn((2, "+FLAGS.SILENT", "\\Flagged"), self.fake.stores)
        self.assertEqual(self.flagged()[2], 1)
        self.assertIsNone(self.db.message(mid).pending_flagged)

    def test_set_server_flag_clears_pending(self):
        self.sync()
        m = self.db.message(self.db.conn().execute("SELECT id FROM messages WHERE uid=1").fetchone()[0])
        self.db.update_message(m.id, flagged=0, pending_flagged=0)
        protocols.close_pool()
        with mock.patch.object(protocols, "imap_connect", return_value=self.fake), \
             mock.patch.object(protocols, "get_password", return_value="x"):
            protocols.set_server_flag(self.db, m, "\\Flagged", False)
        protocols.close_pool()
        self.assertIn((1, "-FLAGS", "\\Flagged"), self.fake.stores)
        self.assertIsNone(self.db.message(m.id).pending_flagged)


class TestFlagUI(unittest.TestCase):
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
        aid = self.db.save_account(Account(name="A", email="a@example.com", storage_dir=self.tmp.name))
        self.ids = [self.db.add_message(Message(account_id=aid, subject=f"M{i}", sender="s@example.com",
                                                date_ts=1700000000 + i, seen=True)) for i in range(3)]
        for patch in (mock.patch.object(main_window, "run_task"),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(lambda: (self.window.close(), self.app.processEvents()))

    def row_of(self, mid):
        return next(r for r in range(self.window.table.rowCount())
                    if self.window.table.item(r, 0).data(self.main_window.ROLE_ID) == mid)

    def test_click_toggles_flag_and_filter(self):
        col = self.main_window.COL["flag"]
        header = self.window.table.horizontalHeader()
        self.assertEqual(header.visualIndex(col), header.visualIndex(self.main_window.COL["att"]) + 1)
        self.window._cell_clicked(self.row_of(self.ids[1]), col)
        self.assertTrue(self.db.message(self.ids[1]).flagged)
        call = self.main_window.run_task.call_args
        self.assertIs(call.args[0], protocols.set_server_flag)
        self.assertEqual(call.args[3:], ("\\Flagged", True))
        self.assertFalse(self.window.table.item(self.row_of(self.ids[1]), col).icon().isNull())
        self.window.f_flagged.click()
        self.app.processEvents()
        shown = [self.window.table.item(r, 0).data(self.main_window.ROLE_ID) for r in range(self.window.table.rowCount())]
        self.assertEqual(shown, [self.ids[1]])
        self.window._cell_clicked(0, col)  # снять
        self.assertFalse(self.db.message(self.ids[1]).flagged)
        self.assertEqual(self.main_window.run_task.call_args.args[3:], ("\\Flagged", False))

    def test_action_flags_all_selected_then_unflags(self):
        self.window.table.selectAll()
        self.window.acts["flag"].trigger()
        self.assertTrue(all(self.db.message(mid).flagged for mid in self.ids))
        self.window.acts["flag"].trigger()
        self.assertFalse(any(self.db.message(mid).flagged for mid in self.ids))


if __name__ == "__main__":
    unittest.main()
