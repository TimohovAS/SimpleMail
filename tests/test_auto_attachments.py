"""Вложения автоматически хранятся в SQLite и доступны без сети и .eml."""
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from mailclient import protocols
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message


def make_mail(files):
    return protocols.build_message(
        Account(email="sender@example.com"), "recipient@example.com", "Attachments", "Offline body",
        attachments=files,
    ).as_bytes()


class TestAutoAttachments(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "mail.db")
        self.addCleanup(self.db.close)
        self.folder = self.root / "mailbox"
        self.account = Account(name="Test", email="me@example.com", storage_dir=str(self.folder))
        self.db.save_account(self.account)

    def add_mail(self, raw, **fields):
        message = parse_message(raw)
        message.account_id = self.account.id
        for name, value in fields.items():
            setattr(message, name, value)
        return self.db.add_message(message)

    def test_sync_stores_all_attachments_in_database_without_opening_message(self):
        files = [("отчёт.pdf", b"PDF"), ("empty.txt", b"")]
        raw = make_mail(files)
        im = mock.Mock()
        im.select.return_value = ("OK", [b"1"])
        im.list.return_value = ("OK", [b'() "/" INBOX'])
        im.response.return_value = ("UIDVALIDITY", [b"42"])
        im.uid.side_effect = [("OK", [b"1"]),
                              ("OK", [(b"1 (UID 1 FLAGS () BODY[] {1}", raw), b")"])]
        with mock.patch.object(protocols, "imap_connect", return_value=im), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            self.assertEqual(protocols.sync_account(self.db, self.account), 1)
        mid = self.db.list_messages(self.account.id, "inbox")[0]["id"]
        self.assertEqual(self.db.message_attachments(mid), files)
        self.assertTrue(self.db.message(mid).attachments_cached)
        self.assertFalse((self.folder / "Вложения").exists())
        rows = self.db.conn().execute("SELECT typeof(data) FROM attachments").fetchall()
        self.assertEqual([row[0] for row in rows], ["blob", "blob"])

    def test_sent_mail_also_stores_attachments(self):
        files = [("sent.txt", b"sent")]
        em = protocols.build_message(self.account, "other@example.com", "Sent", "Body", attachments=files)
        with mock.patch.object(protocols, "smtp_connect") as smtp, \
                mock.patch.object(protocols, "get_password", return_value="test"):
            smtp.return_value.send_message.return_value = {}
            # Проверяется локальная копия; IMAP Sent будет повторён при следующей проверке.
            with mock.patch.object(protocols, "imap_connect", side_effect=ConnectionError("offline")):
                protocols.send_message(self.db, self.account, em)
        mid = self.db.list_messages(self.account.id, "sent")[0]["id"]
        self.assertEqual(self.db.message_attachments(mid), files)

    def test_same_names_and_order_are_preserved_without_duplicates_on_retry(self):
        files = [("file.txt", b"first"), ("file.txt", b"second"), ("FILE.txt", b"third")]
        raw = make_mail(files)
        mid = self.add_mail(raw, uid=1, server_folder="INBOX")
        other = self.add_mail(make_mail([("file.txt", b"other message")]))
        self.assertIsNone(self.add_mail(raw, uid=1, server_folder="INBOX"))
        self.assertEqual(self.db.message_attachments(mid), files)
        self.assertEqual(self.db.message_attachments(other), [("file.txt", b"other message")])
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM attachments").fetchone()[0], 4)

    def test_attachments_survive_reopening_and_unavailable_eml(self):
        files = [("offline.pdf", b"offline data")]
        mid = self.add_mail(make_mail(files))
        Path(self.db.message(mid).raw_path).unlink()
        reopened = Database(self.db.path)
        try:
            self.assertEqual(reopened.message(mid).raw, b"")
            with mock.patch.object(protocols, "imap_connect", side_effect=AssertionError("offline")):
                self.assertEqual(reopened.message_attachments(mid), files)
            self.assertIn("Offline body", reopened.message(mid).body_text)
        finally:
            reopened.close()

    def test_mail_without_storage_folder_uses_database_too(self):
        self.account.storage_dir = ""
        self.db.save_account(self.account)
        files = [("file.txt", b"blob")]
        mid = self.add_mail(make_mail(files))
        self.assertIsNone(self.db.message(mid).raw_path)
        self.assertEqual(self.db.message_attachments(mid), files)

    def test_unnamed_file_and_attached_email_are_saved(self):
        inner = EmailMessage()
        inner["Subject"] = "Forwarded mail"
        inner.set_content("Forwarded body")
        inner.add_attachment(b"inside", maintype="application", subtype="pdf", filename="inside.pdf")
        outer = EmailMessage()
        outer.set_content("Body")
        outer.add_attachment(b"unnamed", maintype="application", subtype="pdf")
        outer.add_attachment(inner, filename="forwarded.eml")
        mid = self.add_mail(outer.as_bytes())
        files = self.db.message_attachments(mid)
        self.assertEqual([name for name, _ in files], ["attachment-1.pdf", "forwarded.eml"])
        self.assertIn(b"Forwarded body", files[1][1])
        self.assertIn(b"inside.pdf", files[1][1])

    def mark_uncached(self, mid):
        with self.db.conn() as c:
            c.execute("DELETE FROM attachments WHERE message_id=?", (mid,))
            c.execute("UPDATE messages SET attachments_cached=0 WHERE id=?", (mid,))

    def test_reading_old_attachments_does_not_write_to_database(self):
        files = [("legacy.pdf", b"legacy")]
        mid = self.add_mail(make_mail(files))
        self.mark_uncached(mid)
        changes = self.db.conn().total_changes
        self.assertEqual(self.db.message_attachments(mid), files)
        self.assertEqual(self.db.message_attachments(mid), files)
        self.assertFalse(self.db.message(mid).attachments_cached)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM attachments").fetchone()[0], 0)
        self.assertEqual(self.db.conn().total_changes, changes)

    def test_attachment_write_failure_rolls_back_entire_message(self):
        raw = make_mail([("file.txt", b"retry")])
        insert = self.db._insert_attachments

        def fail_after_insert(connection, mid, files):
            insert(connection, mid, files)
            raise RuntimeError("simulated write failure")

        with mock.patch.object(self.db, "_insert_attachments", side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError):
                self.add_mail(raw, uid=1, server_folder="INBOX")
        self.assertEqual(self.db.list_messages(self.account.id, "inbox"), [])
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM attachments").fetchone()[0], 0)
        mid = self.add_mail(raw, uid=1, server_folder="INBOX")
        self.assertEqual(self.db.message_attachments(mid), [("file.txt", b"retry")])

    def test_missing_old_eml_is_readable_when_restored_without_data_transfer(self):
        raw = make_mail([("old.txt", b"restored")])
        mid = self.add_mail(raw)
        self.mark_uncached(mid)
        path = Path(self.db.message(mid).raw_path)
        path.unlink()
        self.assertEqual(self.db.message_attachments(mid), [])
        self.assertFalse(self.db.message(mid).attachments_cached)
        path.write_bytes(raw)
        self.assertEqual(self.db.message_attachments(mid), [("old.txt", b"restored")])
        self.assertFalse(self.db.message(mid).attachments_cached)

    def test_delete_message_and_account_remove_their_attachment_blobs(self):
        first = self.add_mail(make_mail([("delete.txt", b"first")]))
        second = self.add_mail(make_mail([("keep.txt", b"second")]))
        self.db.delete_message(first)
        self.assertEqual(self.db.message_attachments(first), [])
        self.assertEqual(self.db.message_attachments(second), [("keep.txt", b"second")])
        self.db.delete_account(self.account.id)
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM attachments").fetchone()[0], 0)

    def test_old_schema_opens_without_attachment_transfer_and_preserves_exports(self):
        raw = make_mail([("old.txt", b"original")])
        mid = self.add_mail(raw)
        exported = self.folder / "Вложения" / "Письмо-legacy" / "old.txt"
        exported.parent.mkdir(parents=True)
        exported.write_bytes(b"edited export")
        with self.db.conn() as c:
            c.execute("DROP TABLE attachments")
            c.execute("ALTER TABLE messages DROP COLUMN attachments_cached")
            c.execute("ALTER TABLE messages ADD COLUMN attachments_saved_dir TEXT NOT NULL DEFAULT ''")
        migrated = Database(self.db.path)
        try:
            self.assertEqual(migrated.message(mid).raw, raw)
            self.assertEqual(migrated.message_attachments(mid), [("old.txt", b"original")])
            self.assertFalse(migrated.message(mid).attachments_cached)
            self.assertEqual(migrated.conn().execute("SELECT count(*) FROM attachments").fetchone()[0], 0)
            self.assertEqual(exported.read_bytes(), b"edited export")
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
