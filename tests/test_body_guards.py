"""A header row must never be sent, trained on or mistaken for an empty body."""
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient import delivery, i18n, mailops, protocols, sync
from mailclient.db import Account, Database, Message
from mailclient.errors import MailOperationError
from mailclient.mailparse import parse_message
from mailclient.spam import SpamFilter
from mailclient.ui import viewer
from mailclient.ui.header import MessageHeader
from test_mailops import FakeIMAP


class BodyFixture(unittest.TestCase):
    def setUp(self):
        i18n.set_language("ru")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(email="me@example.com", storage_dir=self.tmp.name)
        self.db.save_account(self.acc)
        self.raw = protocols.build_message(self.acc, "other@example.com", "Subject", "Complete body",
                                           attachments=[("important.pdf", b"PDF")]).as_bytes()

    def stub(self, folder="inbox"):
        message = parse_message(self.raw)
        message.account_id, message.folder = self.acc.id, folder
        message.server_folder, message.uid, message.uidvalidity = "INBOX", 10, 42
        message.body_downloaded = False
        message.raw = b""
        message.body_text = message.body_html = ""
        message.size = len(self.raw)
        message.id = self.db.add_message(message)
        return message

    def complete(self, db, mid):
        current = db.message(mid)
        message = parse_message(self.raw)
        for field in ("account_id", "folder", "server_folder", "uid", "uidvalidity"):
            setattr(message, field, getattr(current, field))
        db.complete_message_body(mid, message)
        return db.message(mid)


class TestIncompleteMailOperations(BodyFixture):
    def test_move_downloads_before_server_change_and_survives_missing_copyuid(self):
        message = self.stub()
        im = FakeIMAP(self.raw)
        im.emit_copyuid = False
        events = []
        def fetch(db, mid):
            events.append("body")
            self.assertIn(10, im.boxes["INBOX"])
            return self.complete(db, mid)
        with mock.patch.object(sync, "fetch_message_body", side_effect=fetch, create=True) as fetcher, \
                mock.patch.object(mailops, "account_imap", side_effect=lambda *args: nullcontext(im)):
            mailops.move_to_trash(self.db, message.id)
            moved = self.db.message(message.id)
            self.assertTrue(moved.body_downloaded)
            self.assertEqual(moved.raw, self.raw)
            self.assertEqual(moved.folder, "trash")
            self.assertIsNone(moved.uid)
            mailops.restore_message(self.db, message.id)
        fetcher.assert_called_once()
        self.assertEqual(events, ["body"])
        self.assertEqual(self.db.message(message.id).folder, "inbox")
        self.assertEqual(self.db.message_attachments(message.id), [("important.pdf", b"PDF")])

    def test_failed_body_load_never_moves_or_trains_header(self):
        for action in (mailops.move_to_trash, lambda db, mid: mailops.set_spam_state(db, mid, True)):
            with self.subTest(action=action):
                message = self.stub()
                with mock.patch.object(sync, "fetch_message_body", return_value=None, create=True), \
                        mock.patch.object(mailops, "_server_move") as move:
                    with self.assertRaises(MailOperationError):
                        action(self.db, message.id)
                move.assert_not_called()
                unchanged = self.db.message(message.id)
                self.assertEqual(unchanged.folder, "inbox")
                self.assertEqual(unchanged.trained_as, 0)
                self.db.delete_message(message.id)

    def test_permanent_delete_with_known_uid_does_not_download_body(self):
        message = self.stub("trash")
        self.db.update_message(message.id, trash_origin_folder="inbox")
        im = FakeIMAP(self.raw)
        with mock.patch.object(sync, "fetch_message_body", create=True) as fetcher, \
                mock.patch.object(mailops, "account_imap", side_effect=lambda *args: nullcontext(im)):
            mailops.delete_permanently(self.db, message.id)
        fetcher.assert_not_called()
        self.assertIsNone(self.db.message(message.id))
        self.assertNotIn(10, im.boxes["INBOX"])
        self.assertIn(11, im.boxes["INBOX"])

    def test_spam_training_waits_for_complete_body(self):
        message = self.stub()
        with mock.patch.object(sync, "fetch_message_body", side_effect=self.complete, create=True):
            mailops.set_spam_state(self.db, message.id, True)
        self.assertEqual(self.db.get_setting("bayes_spam_docs"), "1")
        self.assertEqual(self.db.message(message.id).trained_as, 1)
        self.assertTrue(self.db.message(message.id).body_downloaded)
        self.assertIsNotNone(self.db.conn().execute("SELECT token FROM bayes_tokens WHERE token='complete'").fetchone())


class TestIncompleteSpam(BodyFixture):
    def test_training_header_is_rejected_without_poisoning_counts(self):
        message = self.stub()
        with self.assertRaises(MailOperationError):
            SpamFilter(self.db).train(message, True)
        self.assertEqual(self.db.get_setting("bayes_spam_docs"), "0")
        self.assertEqual(self.db.message(message.id).trained_as, 0)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM bayes_tokens").fetchone()[0], 0)

    def test_header_classification_preserves_server_state_until_body_arrives(self):
        message = self.stub()
        message.is_spam, message.spam_reason = True, "server spam"
        self.db.add_spam_rule("white", "from", "me@example.com")
        filter = SpamFilter(self.db)
        with mock.patch.object(filter, "bayes_score") as score:
            verdict = filter.classify(message)
        self.assertTrue(verdict.is_spam)
        self.assertIsNone(verdict.score)
        self.assertEqual(verdict.reason, "server spam")
        score.assert_not_called()


class TestIncompleteDelivery(BodyFixture):
    def queued(self):
        return self.db.save_composition(self.acc.id, self.raw, {"to": "other@example.com"}, queued=True)

    def test_outbox_without_body_never_opens_smtp(self):
        mid = self.queued()
        self.db.update_message(mid, body_downloaded=0)
        with mock.patch.object(protocols, "smtp_connect") as connect:
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "failed")
        connect.assert_not_called()
        self.assertEqual(self.db.message(mid).delivery_state, "failed")

    def test_missing_local_raw_never_sends_empty_message(self):
        mid = self.queued()
        self.db.update_message(mid, raw=None, raw_path=None)
        with mock.patch.object(protocols, "smtp_connect") as connect:
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "failed")
        connect.assert_not_called()

    def test_append_requires_body_before_any_server_request(self):
        mid = self.queued()
        self.db.update_message(mid, folder="sent", delivery_state="append_pending", body_downloaded=0)
        with mock.patch.object(protocols, "account_imap") as connect:
            with self.assertRaises(MailOperationError):
                delivery.append_sent(self.db, mid)
        connect.assert_not_called()
        self.assertEqual(self.db.message(mid).delivery_state, "append_pending")


class TestIncompleteBodyViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        i18n.set_language("ru")

    def test_header_actions_disabled_until_same_message_is_complete(self):
        header = MessageHeader()
        self.addCleanup(header.close)
        message = Message(id=1, sender="a@example.com", body_downloaded=False)
        header.show_message(message, [])
        self.assertTrue(all(not button.isEnabled() for button in header._body_buttons))
        self.assertFalse(header.att_btn.isEnabled())
        self.assertTrue(header.thread_btn.isEnabled())
        message.body_downloaded = True
        header.show_message(message, [])
        self.assertTrue(all(button.isEnabled() for button in header._body_buttons))

    def test_single_header_has_loading_placeholder_not_fake_empty_body(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            view = viewer.MessageView()
        self.addCleanup(view.close)
        message = Message(id=1, body_downloaded=False, body_html='<img src="https://example.com/private">')
        view.show_message(message)
        self.assertIn("Содержимое письма ещё загружается", view._page_html)
        self.assertNotIn("https://example.com/private", view._page_html)
        self.assertTrue(view.banner.isHidden())
        self.assertEqual(view._inline, {})

    def test_thread_placeholder_and_state_change_when_same_id_gets_body(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            view = viewer.MessageView()
        self.addCleanup(view.close)
        first = Message(id=1, body_text="Ready message")
        second = Message(id=2, body_downloaded=False)
        view.show_thread(first, [first, second], set())
        self.assertEqual(view.thread_state(), {(1, True), (2, False)})
        self.assertIn("Нажмите на письмо, чтобы загрузить его первым", view._page_html)
        second = Message(id=2, body_text="Arrived body", body_downloaded=True)
        view.show_thread(first, [first, second], set())
        self.assertEqual(view.thread_state(), {(1, True), (2, True)})
        self.assertIn("Arrived body", view._page_html)
        self.assertNotIn("Нажмите на письмо", view._page_html)
        view.clear()
        self.assertEqual(view.thread_state(), set())


if __name__ == "__main__":
    unittest.main()
