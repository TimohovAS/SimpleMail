"""Удаление сохраняет письма в корзине и не затрагивает чужие Deleted UID."""
import imaplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailclient import mailops, protocols
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message
from mailclient.protocols import build_message


class FakeIMAP:
    def __init__(self, raw, caps=b"IMAP4rev1 MOVE UIDPLUS"):
        self.caps = caps
        self.boxes = {"INBOX": {10: raw, 11: b"unrelated"}, "Trash": {5: b"other trash"}}
        self.deleted = {"INBOX": {11}, "Trash": {5}}
        self.validity = {"INBOX": 42, "Trash": 84}
        self.selected = None
        self.mapping = None
        self.emit_copyuid = True
        self.calls = []
        self.fail_command = None
        self.listing = [b'(\\HasNoChildren) "/" INBOX', b'(\\Trash) "/" "Trash"']

    @staticmethod
    def name(value):
        if isinstance(value, bytes):
            value = value.decode()
        return value.strip('"').replace('\\"', '"').replace("\\\\", "\\")

    def capability(self):
        return "OK", [self.caps]

    def list(self, *args):
        return "OK", self.listing

    def create(self, value):
        name = self.name(value)
        self.boxes[name], self.deleted[name], self.validity[name] = {}, set(), 99
        return "OK", [b"created"]

    def select(self, value):
        self.selected = self.name(value)
        if self.selected not in self.boxes:
            return "NO", [b"missing mailbox"]
        return "OK", [str(len(self.boxes[self.selected])).encode()]

    def response(self, code):
        if code == "UIDVALIDITY":
            return code, [str(self.validity[self.selected]).encode()]
        if code == "COPYUID":
            data, self.mapping = self.mapping, None
            return code, [data]

    def uid(self, command, *args):
        self.calls.append((command, self.selected, args))
        if command == self.fail_command:
            return "NO", [b"simulated failure"]
        box = self.boxes[self.selected]
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in box)]
        uid = int(args[0])
        if command == "FETCH":
            if uid not in box:
                return "OK", [None]
            meta = f"1 (UID {uid})".encode()
            return "OK", [(meta, box[uid])] if "BODY.PEEK[]" in args[1] else [meta]
        if command in ("MOVE", "COPY"):
            target = self.name(args[1])
            if uid in box:
                new_uid = max(self.boxes[target], default=0) + 1
                self.boxes[target][new_uid] = box[uid]
                if self.emit_copyuid:
                    self.mapping = f"{self.validity[target]} {uid} {new_uid}".encode()
                if command == "MOVE":
                    del box[uid]
            return "OK", [b"done"]
        if command == "STORE":
            if args[2] == "(\\Deleted)":
                if args[1].startswith("+"):
                    self.deleted[self.selected].add(uid)
                else:
                    self.deleted[self.selected].discard(uid)
            return "OK", [b"stored"]
        if command == "EXPUNGE":
            if uid in self.deleted[self.selected]:
                box.pop(uid, None)
                self.deleted[self.selected].discard(uid)
            return "OK", [b"expunged"]
        raise AssertionError(command)

    def expunge(self):
        raise AssertionError("General EXPUNGE must never be used")

    def logout(self):
        pass


class TestSafeMailOperations(unittest.TestCase):
    def setUp(self):
        protocols.close_pool()  # фиктивные соединения не переходят между тестами
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(email="me@example.com", storage_dir=self.tmp.name)
        self.db.save_account(self.acc)
        self.raw = build_message(self.acc, "other@example.com", "Subject", "Body",
                                 attachments=[("test.pdf", b"PDF")]).as_bytes()
        m = parse_message(self.raw)
        m.account_id, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "INBOX", 10, 42
        self.mid = self.db.add_message(m)
        self.im = FakeIMAP(self.raw)
        patch = mock.patch.object(mailops, "imap_connect", return_value=self.im)
        self.connect = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(protocols.close_pool)
        patch = mock.patch.object(mailops, "get_password", return_value="test")
        patch.start()
        self.addCleanup(patch.stop)

    def test_move_and_restore_keep_local_mail_attachments_and_new_server_uids(self):
        mailops.move_to_trash(self.db, self.mid)
        moved = self.db.message(self.mid)
        self.assertEqual((moved.folder, moved.server_folder, moved.uid, moved.uidvalidity), ("trash", "Trash", 6, 84))
        self.assertEqual(moved.trash_origin_folder, "inbox")
        self.assertNotIn(10, self.im.boxes["INBOX"])
        self.assertEqual(self.im.boxes["Trash"][6], self.raw)
        self.assertEqual(self.db.message_attachments(self.mid), [("test.pdf", b"PDF")])
        mailops.restore_message(self.db, self.mid)
        restored = self.db.message(self.mid)
        self.assertEqual((restored.folder, restored.server_folder, restored.uid, restored.uidvalidity), ("inbox", "INBOX", 12, 42))
        self.assertIsNone(restored.trash_origin_folder)
        self.assertEqual(restored.raw, self.raw)
        self.assertEqual(self.im.boxes["Trash"], {5: b"other trash"})
        self.assertFalse(any(call[0] == "EXPUNGE" for call in self.im.calls))

    def test_copy_uid_expunge_fallback_preserves_unrelated_deleted_messages(self):
        self.im.caps = b"IMAP4rev1 UIDPLUS"
        mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.im.boxes["INBOX"], {11: b"unrelated"})
        self.assertEqual(self.im.deleted["INBOX"], {11})
        self.assertIn(("EXPUNGE", "INBOX", ("10",)), self.im.calls)
        self.assertEqual(self.db.message(self.mid).folder, "trash")

    def test_missing_copyuid_is_resolved_by_exact_local_source_on_restore(self):
        self.im.emit_copyuid = False
        mailops.move_to_trash(self.db, self.mid)
        self.assertIsNone(self.db.message(self.mid).uid)
        mailops.restore_message(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "inbox")
        self.assertIn(self.raw, self.im.boxes["INBOX"].values())
        self.assertEqual(self.im.boxes["Trash"], {5: b"other trash"})

    def test_ambiguous_destination_does_not_restore_wrong_message(self):
        self.im.emit_copyuid = False
        mailops.move_to_trash(self.db, self.mid)
        self.im.boxes["Trash"][7] = self.raw
        with self.assertRaises(RuntimeError):
            mailops.restore_message(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "trash")

    def test_uidvalidity_change_never_changes_server_or_local_message(self):
        self.im.validity["INBOX"] = 43
        with self.assertRaises(RuntimeError):
            mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.im.calls, [])
        self.assertEqual(self.db.message(self.mid).folder, "inbox")

    def test_old_message_validity_is_checked_against_saved_folder_state(self):
        self.db.update_message(self.mid, uidvalidity=None)
        self.db.set_folder_state(self.acc.id, "INBOX", 41, 11)
        with self.assertRaises(RuntimeError):
            mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.im.calls, [])

    def test_unknown_validity_requires_exact_source_before_move(self):
        self.db.update_message(self.mid, uidvalidity=None)
        self.im.boxes["INBOX"][10] = b"another message"
        with self.assertRaises(RuntimeError):
            mailops.move_to_trash(self.db, self.mid)
        self.assertFalse(any(call[0] == "MOVE" for call in self.im.calls))

    def test_nonexistent_uid_is_not_reported_as_moved(self):
        del self.im.boxes["INBOX"][10]
        with self.assertRaises(RuntimeError):
            mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "inbox")

    def test_unsupported_or_rejected_move_keeps_local_message(self):
        for caps, failure in ((b"IMAP4rev1", None), (b"IMAP4rev1 MOVE", "MOVE"),
                              (b"IMAP4rev1 UIDPLUS", "COPY"), (b"IMAP4rev1 UIDPLUS", "STORE")):
            with self.subTest(caps=caps, failure=failure):
                self.im = FakeIMAP(self.raw, caps)
                self.im.fail_command = failure
                self.connect.return_value = self.im
                with self.assertRaises((RuntimeError, imaplib.IMAP4.error)):
                    mailops.move_to_trash(self.db, self.mid)
                self.assertEqual(self.db.message(self.mid).folder, "inbox")
                self.assertEqual(self.im.boxes["INBOX"][10], self.raw)
                self.assertEqual(self.db.message_attachments(self.mid), [("test.pdf", b"PDF")])

    def test_failed_scoped_expunge_restores_source_deleted_flag(self):
        self.im.caps, self.im.fail_command = b"IMAP4rev1 UIDPLUS", "EXPUNGE"
        with self.assertRaises(imaplib.IMAP4.error):
            mailops.move_to_trash(self.db, self.mid)
        self.assertNotIn(10, self.im.deleted["INBOX"])
        self.assertEqual(self.db.message(self.mid).folder, "inbox")

    def test_permanent_delete_only_removes_selected_trash_uid_and_local_blobs(self):
        mailops.move_to_trash(self.db, self.mid)
        mailops.delete_permanently(self.db, self.mid)
        self.assertIsNone(self.db.message(self.mid))
        self.assertEqual(self.db.message_attachments(self.mid), [])
        self.assertEqual(self.im.boxes["Trash"], {5: b"other trash"})
        self.assertEqual(self.im.deleted["Trash"], {5})

    def test_unsupported_permanent_delete_keeps_trash_and_local_copy(self):
        mailops.move_to_trash(self.db, self.mid)
        self.im.caps = b"IMAP4rev1 MOVE"
        with self.assertRaises(RuntimeError):
            mailops.delete_permanently(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertEqual(self.im.boxes["Trash"][6], self.raw)

    def test_failed_permanent_expunge_preserves_local_copy_and_restores_flag(self):
        mailops.move_to_trash(self.db, self.mid)
        self.im.fail_command = "EXPUNGE"
        with self.assertRaises(imaplib.IMAP4.error):
            mailops.delete_permanently(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        self.assertNotIn(6, self.im.deleted["Trash"])

    def test_expected_failure_is_reported_with_actionable_summary(self):
        self.im.caps = b"IMAP4rev1"
        done, errors = mailops.change_messages(self.db, [self.mid], mailops.move_to_trash)
        self.assertEqual(done, [])
        self.assertIn("Сервер не поддерживает", errors[0].summary)
        self.assertEqual(self.db.message(self.mid).folder, "inbox")

    def test_local_sent_message_goes_to_trash_and_restores_without_network(self):
        self.db.update_message(self.mid, folder="sent", server_folder=None, uid=None)
        mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "trash")
        mailops.restore_message(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "sent")
        self.connect.assert_not_called()

    def test_spam_and_not_spam_move_through_discovered_server_junk_folder(self):
        self.im.boxes["Junk"], self.im.deleted["Junk"], self.im.validity["Junk"] = {}, set(), 90
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)", (self.acc.id, "Junk", "spam", "Junk"))
        mailops.set_spam_state(self.db, self.mid, True)
        self.assertEqual(self.db.message(self.mid).folder, "spam")
        self.assertEqual(self.db.message(self.mid).server_folder, "Junk")
        mailops.set_spam_state(self.db, self.mid, False)
        self.assertEqual(self.db.message(self.mid).folder, "inbox")
        self.assertEqual(self.db.message(self.mid).server_folder, "INBOX")
        self.assertFalse(self.db.message(self.mid).is_spam)

    def test_legacy_trash_is_restored_locally_without_using_stale_server_uid(self):
        self.db.update_message(self.mid, folder="trash")
        mailops.restore_message(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).folder, "inbox")
        self.assertIsNone(self.db.message(self.mid).server_folder)
        self.connect.assert_not_called()

    def test_trash_special_use_literal_quoted_and_fallback_names(self):
        cases = [([b'(\\Trash) "/" "Deleted Items"'], "Deleted Items"),
                 ([(b'(\\Trash) "/" {8}', b'My Trash')], "My Trash"),
                 ([b'(\\Noselect \\Trash) "/" "Bad"', b'() "/" Trash'], "Trash"),
                 ([b'() "/" INBOX'], "Trash")]
        for listing, expected in cases:
            with self.subTest(listing=listing):
                self.im.listing = listing
                self.assertEqual(mailops._trash_mailbox(self.im), expected)

    def test_flags_use_current_quoted_trash_folder_and_check_its_validity(self):
        self.im.boxes["Deleted Items"] = self.im.boxes.pop("Trash")
        self.im.deleted["Deleted Items"] = self.im.deleted.pop("Trash")
        self.im.validity["Deleted Items"] = self.im.validity.pop("Trash")
        self.im.listing = [b'(\\Trash) "/" "Deleted Items"']
        original = self.db.message(self.mid)
        mailops.move_to_trash(self.db, self.mid)
        with mock.patch.object(protocols, "imap_connect", return_value=self.im), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.set_server_flag(self.db, original, "\\Seen")
            self.assertIn(("STORE", "Deleted Items", ("6", "+FLAGS", "(\\Seen)")), self.im.calls)
            self.im.validity["Deleted Items"] = 85
            calls = len(self.im.calls)
            with self.assertRaises(mailops.MailOperationError):
                protocols.set_server_flag(self.db, original, "\\Seen")
            self.assertEqual(len(self.im.calls), calls)


if __name__ == "__main__":
    unittest.main()
