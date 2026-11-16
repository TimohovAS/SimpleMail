"""Gmail: «Прочитано» у письма одно на все метки. Отметка в одной папке сразу меняет его копии
в других папках, а не через несколько минут, когда проверка почты перечитает флаги."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient import protocols
from mailclient.db import Account, Database, Message
from mailclient.mailops import mark_seen
from test_core import make_raw
from test_flagged import FlagIMAP


class GmailSeenCopiesCase(unittest.TestCase):
    host = "imap.gmail.com"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.account(self.db.save_account(Account(
            name="G", email="me@gmail.com", username="me@gmail.com", imap_host=self.host, smtp_host="s")))
        raw = make_raw("shop@example.com", "Заказ", "1")
        self.raw = raw
        self.inbox = self.add("inbox", "INBOX", 1)
        self.label = self.add("label:Покупки", "Покупки", 7)
        self.other = self.add("inbox", "INBOX", 2, raw=make_raw("x@example.com", "Другое", "2"))

    def add(self, folder, server_folder, uid, raw=None, seen=False):
        raw = raw or self.raw
        return self.db.add_message(Message(
            account_id=self.acc.id, folder=folder, server_folder=server_folder, uid=uid, uidvalidity=42,
            message_id=next(line.split(b":", 1)[1].strip().decode() for line in raw.splitlines()
                            if line.lower().startswith(b"message-id:")),
            subject="Заказ", sender="shop@example.com", raw=raw, size=len(raw), seen=seen))

    def message(self, mid):
        return self.db.message(mid, with_raw=False)


class TestMarkSeenCopies(GmailSeenCopiesCase):
    def test_copies_in_other_labels_are_marked_at_once(self):
        to_server, changed = mark_seen(self.db, [self.label], True)
        self.assertEqual(to_server, [self.label])
        self.assertEqual(sorted(changed), sorted([self.label, self.inbox]))
        copy = self.message(self.inbox)
        self.assertTrue(copy.seen)
        # До записи флага на сервер копию не сбросит проверка почты, перечитавшая старые FLAGS.
        self.assertEqual(copy.pending_seen, 1)
        self.assertFalse(self.message(self.other).seen)

    def test_unread_is_copied_too(self):
        mark_seen(self.db, [self.label], True)
        to_server, changed = mark_seen(self.db, [self.inbox], False)
        self.assertEqual(to_server, [self.inbox])
        self.assertIn(self.label, changed)
        self.assertFalse(self.message(self.label).seen)
        self.assertEqual(self.message(self.label).pending_seen, 0)

    def test_local_copy_gets_seen_without_server_intent(self):
        local = self.db.add_message(Message(account_id=self.acc.id, folder="archive", message_id=self.message(self.inbox).message_id,
                                            subject="Заказ", sender="shop@example.com", raw=self.raw, size=len(self.raw)))
        mark_seen(self.db, [self.inbox], True)
        self.assertTrue(self.message(local).seen)
        self.assertIsNone(self.message(local).pending_seen)

    def test_selected_copies_are_written_to_server_once(self):
        # Обе копии выбраны (например, в поиске): на сервер флаг пишется одной из них.
        to_server, changed = mark_seen(self.db, [self.inbox, self.label], True)
        self.assertEqual(to_server, [self.inbox])
        self.assertEqual(sorted(changed), sorted([self.inbox, self.label]))

    def test_already_read_message_changes_nothing(self):
        self.db.update_message(self.inbox, seen=1)
        self.assertEqual(mark_seen(self.db, [self.inbox], True), ([], []))
        self.assertFalse(self.message(self.label).seen)

    def test_drafts_are_untouched(self):
        draft = self.db.add_message(Message(account_id=self.acc.id, folder="drafts", message_id=self.message(self.inbox).message_id,
                                            subject="Заказ", sender="me@gmail.com"))
        mark_seen(self.db, [self.inbox], True)
        self.assertFalse(self.message(draft).seen)


class TestOtherServers(GmailSeenCopiesCase):
    host = "mail.example.com"

    def test_copies_on_other_servers_stay_separate_messages(self):
        to_server, changed = mark_seen(self.db, [self.label], True)
        self.assertEqual((to_server, changed), ([self.label], [self.label]))
        self.assertFalse(self.message(self.inbox).seen)
        self.assertIsNone(self.message(self.inbox).pending_seen)


class TestServerWrite(GmailSeenCopiesCase):
    def setUp(self):
        super().setUp()
        self.fake = FlagIMAP({1: self.raw, 7: self.raw}, {})
        connect = mock.patch.object(protocols, "imap_connect", return_value=self.fake)
        connect.start()
        self.addCleanup(connect.stop)
        password = mock.patch.object(protocols, "get_password", return_value="x")
        password.start()
        self.addCleanup(password.stop)
        protocols.close_pool()
        self.addCleanup(protocols.close_pool)

    def test_bulk_write_clears_copy_intent_without_second_store(self):
        to_server, _ = mark_seen(self.db, [self.label], True)
        protocols.set_server_flags(self.db, to_server, "\\Seen", True, queued=True)
        self.assertEqual(self.fake.stores, [(7, "+FLAGS.SILENT", "\\Seen")])
        self.assertIsNone(self.message(self.label).pending_seen)
        self.assertIsNone(self.message(self.inbox).pending_seen)
        self.assertTrue(self.message(self.inbox).seen)

    def test_single_write_clears_copy_intent(self):
        to_server, _ = mark_seen(self.db, [self.inbox], True)
        protocols.set_server_flag(self.db, self.message(self.inbox), "\\Seen", queued=True)
        self.assertEqual([uid for uid, _, _ in self.fake.stores], [1])
        self.assertIsNone(self.message(self.label).pending_seen)

    def test_newer_opposite_intent_of_copy_is_kept(self):
        mark_seen(self.db, [self.label], True)
        self.db.update_message(self.inbox, seen=0, pending_seen=0)  # пользователь тут же снял отметку у копии
        protocols.set_server_flags(self.db, [self.label], "\\Seen", True, queued=True)
        self.assertEqual(self.message(self.inbox).pending_seen, 0)

    def test_failed_write_keeps_copy_intent(self):
        mark_seen(self.db, [self.label], True)
        with mock.patch.object(type(self.fake), "response", lambda im, code: (code, [b"43"])):
            with self.assertRaises(Exception):
                protocols.set_server_flags(self.db, [self.label], "\\Seen", True, queued=True)
        self.assertEqual(self.message(self.inbox).pending_seen, 1)


if __name__ == "__main__":
    unittest.main()
