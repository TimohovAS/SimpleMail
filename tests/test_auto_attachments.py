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
        from test_core import FakeIMAP
        files = [("отчёт.pdf", b"PDF"), ("empty.txt", b"")]
        raw = make_mail(files)
        # Both phases return their real payload length: headers first, full body
        # later in the same background sync, without opening the message.
        im = FakeIMAP({1: raw})
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

    def test_attachment_content_lives_in_eml_only_names_stay_in_database(self):
        # Решение от 2026-11-05: вложения хранятся только в .eml, в базе — имена (поиск, шапка).
        files = [("offline.pdf", b"offline data")]
        mid = self.add_mail(make_mail(files))
        self.assertEqual(self.db.message_attachments(mid), files)
        self.assertEqual([bytes(r[0]) for r in self.db.conn().execute("SELECT data FROM attachments")], [b""])
        Path(self.db.message(mid).raw_path).unlink()
        reopened = Database(self.db.path)
        try:
            self.assertEqual(reopened.message(mid).raw, b"")
            with mock.patch.object(protocols, "imap_connect", side_effect=AssertionError("offline")):
                self.assertEqual(reopened.message_attachments(mid), [])
            self.assertEqual(reopened.attachment_names(mid), ["offline.pdf"])
            self.assertIn("Offline body", reopened.message(mid).body_text)
        finally:
            reopened.close()

    def test_old_attachment_blobs_are_preserved_after_restart_and_compaction(self):
        files = [("a.pdf", b"AAA")]
        kept, cleaned = self.add_mail(make_mail(files)), self.add_mail(make_mail([("b.pdf", b"BBB")]))
        with self.db.conn() as c:  # так хранили версии 0.8–0.9
            c.execute("UPDATE attachments SET data=? WHERE message_id=?", (b"AAA", kept))
            c.execute("UPDATE attachments SET data=? WHERE message_id=?", (b"BBB", cleaned))
        Path(self.db.message(kept).raw_path).unlink()  # .eml пропал — копию в базе не трогаем
        reopened = Database(self.db.path)
        reopened.close()
        self.assertEqual(self.db.message_attachments(kept), files)
        self.assertEqual(self.db.message_attachments(cleaned), [("b.pdf", b"BBB")])
        data = {r[0]: bytes(r[1]) for r in self.db.conn().execute("SELECT message_id, data FROM attachments")}
        self.assertEqual(data, {kept: b"AAA", cleaned: b"BBB"})
        self.db.compact()
        self.assertEqual({r[0]: bytes(r[1]) for r in self.db.conn().execute(
            "SELECT message_id, data FROM attachments")}, data)

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

if __name__ == "__main__":
    unittest.main()
