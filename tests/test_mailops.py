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

    def test_vanished_uid_goes_to_local_trash_without_touching_server(self):
        # Тот же UIDVALIDITY, а UID в папке нет: письмо уже убрали с сервера другой программой.
        del self.im.boxes["INBOX"][10]
        done, errors = mailops.change_messages(self.db, [self.mid], mailops.move_to_trash)
        self.assertEqual((done, errors), ([self.mid], []))
        m = self.db.message(self.mid)
        self.assertEqual((m.folder, m.server_folder, m.uid, m.uidvalidity), ("trash", None, None, None))
        self.assertEqual(m.trash_origin_folder, "inbox")
        self.assertEqual(m.raw, self.raw)
        self.assertFalse(any(call[0] in ("MOVE", "COPY", "STORE") for call in self.im.calls))
        self.assertEqual(self.im.boxes["Trash"], {5: b"other trash"})
        mailops.restore_message(self.db, self.mid)  # восстановление — только локально
        self.assertEqual(self.db.message(self.mid).folder, "inbox")
        self.assertIsNone(self.db.message(self.mid).server_folder)

    def add_junk(self):
        self.im.boxes["Junk"], self.im.deleted["Junk"], self.im.validity["Junk"] = {}, set(), 90
        with self.db.conn() as c:
            c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?)", (self.acc.id, "Junk", "spam", "Junk"))

    def test_message_gone_from_trash_is_restored_or_deleted_locally(self):
        mailops.move_to_trash(self.db, self.mid)
        self.assertEqual(self.db.message(self.mid).server_folder, "Trash")
        self.im.boxes["Trash"].clear()  # корзину на сервере очистила другая программа
        self.im.calls.clear()
        done, errors = mailops.change_messages(self.db, [self.mid], mailops.restore_message)
        self.assertEqual((done, errors), ([self.mid], []))
        m = self.db.message(self.mid)
        self.assertEqual((m.folder, m.server_folder, m.uid, m.trash_origin_folder), ("inbox", None, None, None))
        self.assertEqual(m.raw, self.raw)
        self.assertFalse(any(call[0] in ("MOVE", "COPY", "STORE") for call in self.im.calls))
        # «Удалить навсегда» письма, которого в корзине на сервере уже нет: только локально.
        mailops.move_to_trash(self.db, self.mid)  # локальная копия — в корзину без сервера
        self.db.update_message(self.mid, server_folder="Trash", uid=99, uidvalidity=84, trash_origin_folder="inbox")
        done, errors = mailops.change_messages(self.db, [self.mid], mailops.delete_permanently)
        self.assertEqual((done, errors), ([self.mid], []))
        self.assertIsNone(self.db.message(self.mid))
        self.assertFalse(any(call[0] in ("STORE", "EXPUNGE") for call in self.im.calls))

    def test_message_gone_is_marked_spam_and_not_spam_locally(self):
        self.add_junk()
        del self.im.boxes["INBOX"][10]
        done, errors = mailops.change_messages(self.db, [self.mid], lambda db, mid: mailops.set_spam_state(db, mid, True))
        self.assertEqual((done, errors), ([self.mid], []))
        m = self.db.message(self.mid)
        self.assertEqual((m.folder, m.server_folder, m.is_spam), ("spam", None, True))
        self.assertEqual(self.im.boxes["Junk"], {})
        self.db.update_message(self.mid, server_folder="Junk", uid=7, uidvalidity=90)  # а теперь пропало из «Спама»
        done, errors = mailops.change_messages(self.db, [self.mid], lambda db, mid: mailops.set_spam_state(db, mid, False))
        self.assertEqual((done, errors), ([self.mid], []))
        m = self.db.message(self.mid)
        self.assertEqual((m.folder, m.server_folder, m.is_spam), ("inbox", None, False))
        self.assertFalse(any(call[0] in ("MOVE", "COPY") for call in self.im.calls))

    def test_nonexistent_legacy_uid_is_not_reported_as_moved(self):
        # Без UIDVALIDITY отсутствие UID не доказывает, что письма нет: ничего не меняем.
        self.db.update_message(self.mid, uidvalidity=None)
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


class TestGmailLabelCopies(unittest.TestCase):
    """Одно письмо Gmail во «Входящих» и в метке: удалённое, оно пропадает из всех меток."""

    def setUp(self):
        protocols.close_pool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(email="me@gmail.com", imap_host="imap.gmail.com", storage_dir=self.tmp.name)
        self.db.save_account(self.acc)
        self.raw = build_message(self.acc, "other@example.com", "Pinterest", "Body").as_bytes()
        self.inbox = self.add("inbox", "INBOX", 10, 42)
        self.label = self.add("remote:Label", "Label", 3, 15)
        self.im = FakeIMAP(self.raw)
        self.im.boxes["Label"], self.im.deleted["Label"], self.im.validity["Label"] = {3: self.raw}, set(), 15
        for target, value in ((mailops, self.im),):
            patch = mock.patch.object(target, "imap_connect", return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(mailops, "get_password", return_value="test")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(protocols.close_pool)

    def add(self, folder, server_folder, uid, validity):
        m = parse_message(self.raw)
        m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = self.acc.id, folder, server_folder, uid, validity
        return self.db.add_message(m)

    def test_deleting_gmail_message_removes_its_label_copies(self):
        mailops.move_to_trash(self.db, self.inbox)
        self.assertEqual(self.db.message(self.inbox).folder, "trash")
        self.assertIsNone(self.db.message(self.label))

    def test_other_servers_keep_copies_in_other_folders(self):
        self.acc.imap_host = "imap.example.com"
        self.db.save_account(self.acc)
        mailops.move_to_trash(self.db, self.inbox)
        self.assertEqual(self.db.message(self.label).folder, "remote:Label")

    def test_stale_label_copy_of_trashed_message_is_removed_without_error(self):
        # Письмо удалено из «Входящих» (в Gmail — из всех меток), а копия в метке ещё в списке.
        self.db.update_message(self.inbox, folder="trash", server_folder="Trash", uid=6, uidvalidity=84,
                               trash_origin_folder="inbox", trash_origin_server_folder="INBOX")
        del self.im.boxes["Label"][3]
        done, errors = mailops.change_messages(self.db, [self.label], mailops.move_to_trash)
        self.assertEqual((done, errors), ([self.label], []))
        self.assertIsNone(self.db.message(self.label))
        self.assertEqual(self.db.message(self.inbox).folder, "trash")
        self.assertFalse(any(call[0] in ("MOVE", "COPY", "STORE") for call in self.im.calls))

    def test_different_message_with_same_message_id_is_not_a_copy(self):
        other = build_message(self.acc, "other@example.com", "Pinterest", "Другой текст").as_bytes()
        m = parse_message(other)
        m.message_id = self.db.message(self.inbox).message_id
        m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = self.acc.id, "remote:Other", "Other", 7, 16
        different = self.db.add_message(m)
        mailops.move_to_trash(self.db, self.inbox)
        self.assertIsNotNone(self.db.message(different))
        self.assertIsNone(self.db.message(self.label))

    def test_sync_drops_vanished_copy_instead_of_archiving_a_duplicate(self):
        from mailclient import sync
        empty = mock.Mock()
        empty.select.return_value = ("OK", [b"0"])
        empty.response.return_value = ("UIDVALIDITY", [b"15"])
        empty.uid.return_value = ("OK", [b""])
        folder = sync.ServerFolder("Label", "remote:Label", "Label", set())
        sync.sync_mailbox(self.db, self.db.account(self.acc.id), empty, folder, lambda text: None)
        self.assertIsNone(self.db.message(self.label))  # копия во «Входящих» на сервере осталась
        self.assertEqual(self.db.message(self.inbox).folder, "inbox")
        # Последняя копия письма по-прежнему сохраняется в локальном архиве.
        empty.response.return_value = ("UIDVALIDITY", [b"42"])
        sync.sync_mailbox(self.db, self.db.account(self.acc.id), empty, sync.ServerFolder("INBOX", "inbox", "INBOX", set()),
                          lambda text: None)
        self.assertEqual(self.db.message(self.inbox).folder, "archive")

    def make_stub(self, mid):
        """Копия, у которой скачан только заголовок (размер — из RFC822.SIZE)."""
        self.db.update_message(mid, raw=None, raw_path=None, body_downloaded=0, body_text="", body_html="",
                               size=len(self.raw))

    def sync_empty(self, name, view, validity):
        from mailclient import sync
        empty = mock.Mock()
        empty.select.return_value = ("OK", [b"0"])
        empty.response.return_value = ("UIDVALIDITY", [str(validity).encode()])
        empty.uid.return_value = ("OK", [b""])
        sync.sync_mailbox(self.db, self.db.account(self.acc.id), empty, sync.ServerFolder(name, view, name, set()),
                          lambda text: None)

    def test_downloaded_body_is_not_dropped_for_a_header_only_copy(self):
        # Во «Входящих» письмо скачано целиком, в метке — только заголовок того же размера.
        # Письмо удалили на сервере совсем: содержимое должно остаться в локальном архиве.
        self.make_stub(self.label)
        path = Path(self.db.message(self.inbox).raw_path)
        self.sync_empty("INBOX", "inbox", 42)
        m = self.db.message(self.inbox)
        self.assertEqual((m.folder, m.server_folder), ("archive", None))
        self.assertEqual(m.raw, self.raw)
        self.assertTrue(path.exists())
        self.sync_empty("Label", "remote:Label", 15)
        self.assertIsNone(self.db.message(self.label))  # заголовок — дубль архивной копии
        self.assertEqual(self.db.message(self.inbox).raw, self.raw)

    def test_header_only_copy_is_dropped_when_full_copy_remains(self):
        self.make_stub(self.label)
        self.sync_empty("Label", "remote:Label", 15)
        self.assertIsNone(self.db.message(self.label))
        self.assertEqual(self.db.message(self.inbox).folder, "inbox")

    def test_archived_duplicate_is_removed_when_label_copy_gets_its_body(self):
        # Письмо убрали из «Входящих» (архив Gmail), в метке оно осталось — пока заголовком.
        self.make_stub(self.label)
        self.sync_empty("INBOX", "inbox", 42)
        self.assertEqual(self.db.message(self.inbox).folder, "archive")
        full = parse_message(self.raw)
        full.account_id, full.folder, full.server_folder, full.uid, full.uidvalidity = \
            self.acc.id, "remote:Label", "Label", 3, 15
        self.assertTrue(self.db.complete_message_body(self.label, full))
        mailops.drop_archived_duplicates(self.db, self.label, full)
        self.assertIsNone(self.db.message(self.inbox))
        self.assertEqual(self.db.message(self.label).raw, self.raw)

    def test_trash_with_header_only_copy_keeps_the_body(self):
        # «В корзину» письма, которого уже нет на сервере, когда другая копия — только заголовок.
        self.make_stub(self.label)
        del self.im.boxes["INBOX"][10]
        done, errors = mailops.change_messages(self.db, [self.inbox], mailops.move_to_trash)
        self.assertEqual((done, errors), ([self.inbox], []))
        m = self.db.message(self.inbox)
        self.assertEqual((m.folder, m.server_folder), ("trash", None))
        self.assertEqual(m.raw, self.raw)


class TestAutoSpamMessageGone(unittest.TestCase):
    """Автоперенос спама в конце проверки: пропавшее письмо не валит проверку."""

    def setUp(self):
        from test_complete_features import Fixture, FolderIMAP, raw_mail
        self.fixture = Fixture("setUp")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.acc = self.fixture.db, self.fixture.acc

        class JunkIMAP(FolderIMAP):
            def capability(self):
                return "OK", [b"IMAP4rev1 MOVE UIDPLUS"]

            def list(self, *args):
                status, data = super().list(*args)
                return status, [line.replace(b"() \"/\" \"Junk\"", b"(\\Junk) \"/\" \"Junk\"") for line in data]

        self.im = JunkIMAP({"INBOX": {1: (raw_mail("Spam offer"), "")}, "Junk": {}, "Trash": {}})
        protocols.imap_connect.return_value = self.im

    def test_vanished_spam_goes_to_local_spam_and_trash_cleanup_still_runs(self):
        from mailclient import sync
        sync.synchronize(self.db, self.acc)
        mid = self.db.conn().execute("SELECT id FROM messages WHERE folder='inbox'").fetchone()["id"]
        self.db.update_message(mid, is_spam=1)
        with mock.patch.object(mailops, "_server_move", side_effect=mailops.MessageGone("gone")), \
                mock.patch.object(mailops, "purge_old_trash") as purge:
            sync.synchronize(self.db, self.acc)
        purge.assert_called_once()
        m = self.db.message(mid)
        self.assertEqual((m.folder, m.server_folder), ("spam", None))

    def test_other_move_error_is_reported_after_trash_cleanup(self):
        from mailclient import sync
        sync.synchronize(self.db, self.acc)
        mid = self.db.conn().execute("SELECT id FROM messages WHERE folder='inbox'").fetchone()["id"]
        self.db.update_message(mid, is_spam=1)
        with mock.patch.object(mailops, "_server_move", side_effect=RuntimeError("boom")), \
                mock.patch.object(mailops, "purge_old_trash") as purge:
            with self.assertRaises(mailops.MailOperationError):
                sync.synchronize(self.db, self.acc)
        purge.assert_called_once()
        self.assertEqual(self.db.message(mid).folder, "inbox")


if __name__ == "__main__":
    unittest.main()
