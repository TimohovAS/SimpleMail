"""Значки «ответили» / «переслали»: флаги IMAP \\Answered и $Forwarded в обе стороны."""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import delivery, protocols
from mailclient.db import MARK_ANSWERED, MARK_FORWARDED, Account, Database, Message
from mailclient.mailparse import parse_message
from test_core import make_raw
from test_flagged import FlagIMAP

BOTH = MARK_ANSWERED | MARK_FORWARDED


class RefusingKeywords(FlagIMAP):
    """Сервер без пользовательских ключевых слов: $Forwarded не принимает."""
    def uid(self, cmd, *args):
        if cmd == "STORE" and args[2].startswith("($"):
            self.stores.append((int(args[0]), args[1], args[2].strip("()")))
            return "NO", [b"Keywords not supported"]
        return super().uid(cmd, *args)


class MarksCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.account(self.db.save_account(Account(
            name="T", email="me@example.com", username="me", imap_host="i", smtp_host="s")))
        self.fake = FlagIMAP({1: make_raw("a@x.ru", "Один", "1"), 2: make_raw("b@x.ru", "Два", "2"),
                              3: make_raw("c@x.ru", "Три", "3")},
                             {1: {"\\Answered"}, 2: {"$Forwarded"}, 3: {"\\Answered", "$forwarded"}})
        self.addCleanup(protocols.close_pool)

    def connected(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(protocols, "imap_connect", return_value=self.fake))
        stack.enter_context(mock.patch.object(protocols, "get_password", return_value="x"))
        return stack

    def sync(self):
        with self.connected():
            protocols.sync_account(self.db, self.acc)

    def marks(self):
        return {r["uid"]: r["marks"] for r in self.db.conn().execute("SELECT uid, marks FROM messages")}

    def mid(self, uid):
        return self.db.conn().execute("SELECT id FROM messages WHERE uid=?", (uid,)).fetchone()[0]


class TestServerFlags(MarksCase):
    def test_flags_of_new_and_known_messages_arrive(self):
        self.sync()
        self.assertEqual(self.marks(), {1: MARK_ANSWERED, 2: MARK_FORWARDED, 3: BOTH})  # регистр не важен
        self.fake.flags = {1: {"$Forwarded"}, 2: set()}  # другой клиент переслал письмо 1
        self.sync()
        # отметки только добавляются: сервер без $Forwarded не стирает значок
        self.assertEqual(self.marks(), {1: BOTH, 2: MARK_FORWARDED, 3: BOTH})

    def test_add_mark_covers_copies_and_push_clears_pending(self):
        self.sync()
        source = self.db.message(self.mid(2))
        copy = Message(**{**source.__dict__, "id": None, "folder": "archive", "server_folder": "Archive", "uid": 7,
                          "marks": 0})
        copy_id = self.db.add_message(copy)
        local = self.db.add_message(Message(account_id=self.acc.id, subject="без сервера", message_id="<l@x>"))
        self.assertEqual(sorted(self.db.add_mark(source.id, MARK_ANSWERED)), sorted([source.id, copy_id]))
        self.assertEqual(self.db.add_mark(source.id, MARK_ANSWERED), [])  # уже отмечено
        self.assertEqual((self.db.message(copy_id).marks, self.db.message(copy_id).pending_marks),
                         (MARK_ANSWERED, MARK_ANSWERED))
        self.db.add_mark(local, MARK_FORWARDED)
        self.assertEqual((self.db.message(local).marks, self.db.message(local).pending_marks), (MARK_FORWARDED, 0))
        with self.connected():
            protocols.push_marks(self.db, [source.id])
        self.assertIn((2, "+FLAGS.SILENT", "\\Answered"), self.fake.stores)
        self.assertEqual(self.db.message(source.id).pending_marks, 0)

    def test_pending_marks_are_pushed_by_next_sync(self):
        self.sync()
        mid = self.mid(1)
        with mock.patch.object(protocols, "account_imap", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                protocols.mark_and_push(self.db, mid, MARK_FORWARDED)
        self.assertEqual(self.db.message(mid).pending_marks, MARK_FORWARDED)
        self.sync()
        self.assertIn((1, "+FLAGS.SILENT", "$Forwarded"), self.fake.stores)
        self.assertEqual(self.db.message(mid).pending_marks, 0)
        self.assertEqual(self.marks()[1], BOTH)

    def test_server_without_keywords_keeps_local_mark_and_syncs(self):
        self.fake = RefusingKeywords(self.fake.messages, {})
        self.sync()
        mid = self.mid(1)
        self.db.add_mark(mid, MARK_FORWARDED)
        self.sync()  # отказ по $Forwarded не ломает проверку почты
        self.assertIn((1, "+FLAGS.SILENT", "$Forwarded"), self.fake.stores)
        self.assertEqual((self.db.message(mid).marks, self.db.message(mid).pending_marks), (MARK_FORWARDED, 0))


class TestMarkAfterSending(MarksCase):
    def send(self, em, state):
        mid = self.db.save_composition(self.acc.id, em.as_bytes(), state, queued=True)
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with self.connected(), mock.patch.object(protocols, "smtp_connect", return_value=smtp), \
                mock.patch.object(delivery, "append_sent"):
            return delivery.deliver_outbox(self.db, mid)

    def test_reply_and_forward_mark_source_on_server(self):
        self.sync()
        self.fake.flags, self.fake.stores = {}, []
        self.db.conn().execute("UPDATE messages SET marks=0")
        self.db.conn().commit()
        first, second = self.db.message(self.mid(1)), self.db.message(self.mid(2))
        reply = protocols.build_message(self.acc, "a@x.ru", "Re: Один", "ok", in_reply_to=first.message_id)
        self.assertEqual(self.send(reply, {"mark_source": [first.id, MARK_ANSWERED]}), "sent")
        forward = protocols.build_message(self.acc, "z@x.ru", "Fwd: Два", "см.")
        self.assertEqual(self.send(forward, {"mark_source": [second.id, MARK_FORWARDED]}), "sent")
        self.assertEqual(self.fake.stores, [(1, "+FLAGS.SILENT", "\\Answered"), (2, "+FLAGS.SILENT", "$Forwarded")])
        self.assertEqual(self.db.message(first.id).marks, MARK_ANSWERED)
        self.assertEqual(self.db.message(second.id).pending_marks, 0)

    def test_reply_without_state_found_by_in_reply_to(self):
        self.sync()
        self.db.conn().execute("UPDATE messages SET marks=0")
        self.db.conn().commit()
        first = self.db.message(self.mid(1))
        reply = protocols.build_message(self.acc, "a@x.ru", "Re: Один", "ok", in_reply_to=first.message_id)
        self.assertEqual(self.send(reply, {}), "sent")
        self.assertEqual(self.db.message(first.id).marks, MARK_ANSWERED)
        self.assertEqual(self.marks()[2], 0)

    def test_no_mark_when_not_sent_and_offline_server_is_retried(self):
        self.sync()
        self.db.conn().execute("UPDATE messages SET marks=0")
        self.db.conn().commit()
        first = self.db.message(self.mid(1))
        reply = protocols.build_message(self.acc, "a@x.ru", "Re: Один", "ok", in_reply_to=first.message_id)
        mid = self.db.save_composition(self.acc.id, reply.as_bytes(), {"mark_source": [first.id, MARK_ANSWERED]},
                                       queued=True)
        with mock.patch.object(protocols, "smtp_connect", side_effect=ConnectionError("offline")):
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "queued")
        self.assertEqual(self.db.message(first.id).marks, 0)  # письмо ещё не ушло
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp), \
                mock.patch.object(protocols, "get_password", return_value="x"), \
                mock.patch.object(protocols, "account_imap", side_effect=OSError("imap offline")), \
                mock.patch.object(delivery, "append_sent"):
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "sent")
        self.assertEqual((self.db.message(first.id).marks, self.db.message(first.id).pending_marks),
                         (MARK_ANSWERED, MARK_ANSWERED))


class TestMarksUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mailclient.ui import compose_dialog, main_window, viewer
        self.main_window, self.compose_dialog = main_window, compose_dialog
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        aid = self.db.save_account(Account(name="A", email="a@example.com", storage_dir=self.tmp.name))
        self.ids = []
        for i, marks in enumerate((0, MARK_ANSWERED, MARK_FORWARDED, BOTH)):
            m = parse_message(make_raw("s@example.com", f"M{i}", f"text {i}"))
            m.account_id, m.date_ts, m.seen, m.marks = aid, 1700000000 + i, True, marks
            self.ids.append(self.db.add_message(m))
        for patch in (mock.patch.object(main_window, "run_task"),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                      mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()
        self.addCleanup(lambda: (self.window.close(), self.app.processEvents()))

    def subject_item(self, mid):
        table, mw = self.window.table, self.main_window
        row = next(r for r in range(table.rowCount()) if table.item(r, 0).data(mw.ROLE_ID) == mid)
        return table.item(row, mw.COL["subject"])

    def test_subject_shows_mark_icon_and_tooltip(self):
        plain, answered, forwarded, both = (self.subject_item(mid) for mid in self.ids)
        self.assertTrue(plain.icon().isNull())
        self.assertEqual(plain.toolTip(), "")
        for item in (answered, forwarded, both):
            self.assertFalse(item.icon().isNull())
        self.assertEqual(answered.toolTip(), "Вы ответили на это письмо")
        self.assertEqual(forwarded.toolTip(), "Письмо переслано")
        self.assertEqual(both.toolTip(), "Вы ответили на это письмо и переслали его")

    def open_compose(self, action):
        self.window._shown = self.db.message(self.ids[0])
        with mock.patch.object(self.main_window, "ComposeDialog") as dialog:
            action()
        return dialog.call_args.kwargs

    def test_reply_forward_and_quick_reply_remember_source(self):
        source = self.ids[0]
        self.assertEqual(self.open_compose(self.window._reply)["mark_source"], [source, MARK_ANSWERED])
        self.assertEqual(self.open_compose(self.window._reply_all)["mark_source"], [source, MARK_ANSWERED])
        self.assertEqual(self.open_compose(self.window._forward)["mark_source"], [source, MARK_FORWARDED])
        _, state = self.window._quick_message(self.db.message(source), "ok")
        self.assertEqual(state["mark_source"], [source, MARK_ANSWERED])

    def test_compose_draft_keeps_source(self):
        dlg = self.compose_dialog.ComposeDialog(self.db, account_id=self.db.message(self.ids[0]).account_id,
                                                to="x@example.com", subject="Fwd: M0",
                                                mark_source=[self.ids[0], MARK_FORWARDED])
        self.addCleanup(dlg.deleteLater)
        state, em = dlg._composition()
        self.assertEqual(state["mark_source"], [self.ids[0], MARK_FORWARDED])
        draft = self.db.save_composition(self.db.message(self.ids[0]).account_id, em.as_bytes(), state)
        reopened = self.compose_dialog.ComposeDialog(self.db, draft_id=draft)
        self.addCleanup(reopened.deleteLater)
        self.assertEqual(reopened.mark_source, [self.ids[0], MARK_FORWARDED])
        dlg._dirty = reopened._dirty = False


if __name__ == "__main__":
    unittest.main()
