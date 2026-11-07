"""Folder summaries and metadata indexing avoid repeating payload work."""
import tempfile
import unittest
from email.message import EmailMessage, Message as EmailPart
from pathlib import Path
from unittest import mock

from mailclient.db import Account, Database, Message
from mailclient.mailparse import parse_message


class TestDatabasePerformance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.account = Account(name="One", email="one@example.com")
        self.db.save_account(self.account)

    def test_one_summary_matches_all_folder_views_and_tray_after_changes(self):
        other = Account(name="Two", email="two@example.com")
        self.db.save_account(other)
        views = ("inbox", "spam", "sent", "drafts", "remote:Projects")
        mids = []
        for aid in (self.account.id, other.id):
            for folder in views:
                for seen in (False, True):
                    for is_spam in (False, True):
                        mids.append(self.db.add_message(Message(account_id=aid, folder=folder,
                                                               seen=seen, is_spam=is_spam)))
        self.db.update_message(mids[0], seen=True)
        self.db.update_message(mids[1], folder="remote:Projects")
        self.db.delete_message(mids[-1])
        statements = []
        self.db.conn().set_trace_callback(statements.append)
        try:
            counts = self.db.unread_counts()
        finally:
            self.db.conn().set_trace_callback(None)
        self.assertEqual(sum(statement.lstrip().upper().startswith("SELECT") for statement in statements), 1)
        for aid in (self.account.id, other.id, 999):
            for view in (*views, "remote:Empty"):
                self.assertEqual(counts.get((aid, view), 0), self.db.unread_count(aid, view))
        self.assertEqual(counts[(None, "inbox")], self.db.unread_count(None, "inbox"))

    def test_empty_summary_has_zero_tray_count(self):
        self.assertEqual(self.db.unread_counts(), {(None, "inbox"): 0})

    def test_saving_and_completing_mail_indexes_names_without_decoding_attachments(self):
        em = EmailMessage()
        em["From"] = "sender@example.com"
        em["To"] = "one@example.com"
        em["Subject"] = "Payload stays in the original message"
        em.set_content("Body")
        payload = b"binary attachment data" * 100
        em.add_attachment(payload, maintype="application", subtype="octet-stream", filename="report.bin")
        raw = em.as_bytes()
        full = parse_message(raw)
        full.account_id = self.account.id
        full.server_folder, full.uidvalidity, full.uid = "INBOX", 10, 1
        header = Message(account_id=self.account.id, server_folder="INBOX", uidvalidity=10,
                         uid=2, body_downloaded=False)
        header_id = self.db.add_message(header)
        original = EmailPart.get_payload

        def no_binary_decode(part, *args, **kwargs):
            if kwargs.get("decode") and part.get_content_maintype() == "application":
                raise AssertionError("Metadata indexing decoded a binary attachment")
            return original(part, *args, **kwargs)

        with mock.patch.object(EmailPart, "get_payload", no_binary_decode):
            mid = self.db.add_message(full)
            full.uid = 2
            self.assertTrue(self.db.complete_message_body(header_id, full))
            draft_id = self.db.save_composition(self.account.id, raw, {})
            self.db.save_composition(self.account.id, raw, {}, message_id=draft_id)
        for saved_id in (mid, header_id, draft_id):
            self.assertEqual(self.db.attachment_names(saved_id), ["report.bin"])
            self.assertEqual(self.db.message_attachments(saved_id), [("report.bin", payload)])


if __name__ == "__main__":
    unittest.main()
