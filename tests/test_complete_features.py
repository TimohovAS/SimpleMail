"""Сценарии полной синхронизации, поиска, очереди и резервного восстановления."""
import email
import os
import base64
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mailclient import backup, delivery, protocols, sync
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtCore import QBuffer, QIODevice, QUrl
from mailclient.ui import main_window, viewer
from mailclient.ui.compose_dialog import ComposeDialog


def raw_mail(subject="Тема", body="Текст", files=None):
    return protocols.build_message(Account(email="sender@example.com"), "me@example.com", subject, body,
                                   attachments=files).as_bytes()


class FolderIMAP:
    def __init__(self, boxes=None):
        self.boxes = boxes or {"INBOX": {}, "Sent Items": {}, "Trash": {}, "Projects": {}}
        self.validity = {name: i + 10 for i, name in enumerate(self.boxes)}
        self.selected = None
        self.appenduid = None
        self.append_count = 0
        self.lose_append_response = False
        self.omit_uid = None

    def list(self, *args):
        flags = {"Sent Items": b"\\Sent", "Trash": b"\\Trash"}
        return "OK", [b'(' + flags.get(name, b"") + b') "/" "' + name.encode() + b'"' for name in self.boxes]

    def select(self, name, readonly=False):
        if isinstance(name, bytes):
            name = name.decode()
        self.selected = name.strip('"')
        return "OK", [str(len(self.boxes[self.selected])).encode()]

    def response(self, code):
        if code == "UIDVALIDITY":
            return code, [str(self.validity[self.selected]).encode()]
        return code, [self.appenduid]

    def uid(self, cmd, *args):
        box = self.boxes[self.selected]
        if cmd == "SEARCH":
            ids = list(box)
            if "HEADER" in args:
                value = args[-1].decode().strip('"')
                ids = [uid for uid in ids if email.message_from_bytes(box[uid][0]).get("Message-ID") == value]
            return "OK", [" ".join(map(str, sorted(ids))).encode()]
        if cmd == "FETCH":
            data = []
            uids = sorted(box) if str(args[0]) == "1:*" else map(int, str(args[0]).split(","))
            for uid in uids:
                if uid not in box or uid == self.omit_uid:
                    continue
                raw, flags = box[uid]
                meta = f"1 (UID {uid} FLAGS ({flags})".encode()
                data.append((meta + b" BODY[] {1}", raw) if "BODY.PEEK[]" in args[1] else meta + b")")
            return "OK", data
        if cmd == "STORE":
            uid = int(args[0])
            raw, flags = box[uid]
            flag_set = set(flags.split())
            if args[1].startswith("+"):
                flag_set.add("\\Seen")
            else:
                flag_set.discard("\\Seen")
            box[uid] = (raw, " ".join(flag_set))
            return "OK", [b"stored"]
        raise AssertionError(cmd)

    def append(self, name, flags, date, raw):
        name = name.decode().strip('"')
        uid = max(self.boxes[name], default=0) + 1
        self.boxes[name][uid] = (raw, "\\Seen")
        self.appenduid = f"{self.validity[name]} {uid}".encode()
        self.append_count += 1
        if self.lose_append_response:
            self.lose_append_response = False
            raise ConnectionError("response lost after append")
        return "OK", [b"saved"]

    def logout(self):
        pass


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "data" / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(name="Test", email="me@example.com", storage_dir=str(self.root / "store"))
        self.db.save_account(self.acc)
        self.im = FolderIMAP()
        patch = mock.patch.object(protocols, "imap_connect", return_value=self.im)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(protocols, "get_password", return_value="test")
        patch.start(); self.addCleanup(patch.stop)

    def add(self, raw, folder="inbox"):
        m = parse_message(raw)
        m.account_id, m.folder = self.acc.id, folder
        return self.db.add_message(m)


class TestFullSync(Fixture):
    def test_removed_server_folder_becomes_offline_archive_and_disappears_from_tree_data(self):
        raw = raw_mail("Project copy")
        self.im.boxes["Projects"][1] = (raw, "")
        protocols.sync_account(self.db, self.acc)
        mid = self.db.list_messages(self.acc.id, "remote:Projects")[0]["id"]
        del self.im.boxes["Projects"]
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.message(mid).folder, "archive")
        self.assertEqual(self.db.message(mid).raw, raw)
        self.assertNotIn("Projects", [row["name"] for row in self.db.server_folders(self.acc.id)])

    def test_legacy_uid_without_generation_is_not_assigned_to_different_server_content(self):
        raw = raw_mail("Old untracked content")
        mid = self.add(raw)
        self.db.update_message(mid, server_folder="INBOX", uid=1, uidvalidity=None)
        self.db.set_folder_state(self.acc.id, "INBOX", self.im.validity["INBOX"], 1)
        self.im.boxes["INBOX"][1] = (raw_mail("Different server message"), "")
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.message(mid).folder, "archive")
        self.assertEqual(self.db.message(mid).raw, raw)
        self.assertEqual(self.db.list_messages(self.acc.id, "inbox")[0]["subject"], "Different server message")

    def test_existing_local_sent_copy_is_linked_to_provider_copy_without_duplicates(self):
        raw = raw_mail("Already sent")
        mid = self.add(raw, "sent")
        server_raw = b"Received: by provider\r\n" + raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        self.im.boxes["Sent Items"][9] = (server_raw, "\\Seen")
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(len(self.db.list_messages(self.acc.id, "sent")), 1)
        self.assertEqual(self.db.message(mid).uid, 9)
        self.assertEqual(self.db.message(mid).raw, server_raw)

    def test_offline_read_change_is_sent_to_server_on_next_sync(self):
        self.im.boxes["INBOX"][1] = (raw_mail(), "")
        protocols.sync_account(self.db, self.acc)
        mid = self.db.list_messages(self.acc.id, "inbox")[0]["id"]
        self.db.update_message(mid, seen=1, pending_seen=1)
        protocols.sync_account(self.db, self.acc)
        self.assertIn("\\Seen", self.im.boxes["INBOX"][1][1])
        self.assertTrue(self.db.message(mid).seen)
        self.assertIsNone(self.db.message(mid).pending_seen)

    def test_discovers_all_folders_and_syncs_phone_flags_and_moves(self):
        raw = raw_mail(files=[("doc.pdf", b"PDF")])
        self.im.boxes["INBOX"][1] = (raw, "")
        self.im.boxes["Projects"][7] = (raw_mail("Project"), "")
        self.assertEqual(protocols.sync_account(self.db, self.acc), 2)
        mid = self.db.list_messages(self.acc.id, "inbox")[0]["id"]
        self.assertEqual(len(self.db.list_messages(self.acc.id, "remote:Projects")), 1)
        self.im.boxes["INBOX"][1] = (raw, "\\Seen")
        protocols.sync_account(self.db, self.acc)
        self.assertTrue(self.db.message(mid).seen)
        del self.im.boxes["INBOX"][1]
        self.im.boxes["Trash"][8] = (raw, "\\Seen")
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.message(mid).folder, "trash")
        self.assertEqual(self.db.message(mid).server_folder, "Trash")
        self.assertEqual(self.db.message_attachments(mid), [("doc.pdf", b"PDF")])

    def test_uidvalidity_reset_keeps_old_offline_copy_and_downloads_reused_uid(self):
        original = raw_mail("Original")
        self.im.boxes["INBOX"][1] = (original, "")
        protocols.sync_account(self.db, self.acc)
        mid = self.db.list_messages(self.acc.id, "inbox")[0]["id"]
        self.im.validity["INBOX"] += 1
        self.im.boxes["INBOX"][1] = (raw_mail("Replacement"), "")
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(self.db.message(mid).folder, "archive")
        self.assertEqual(self.db.message(mid).raw, original)
        self.assertEqual(self.db.list_messages(self.acc.id, "inbox")[0]["subject"], "Replacement")

    def test_history_ignores_initial_limit_and_resumes_after_cancel(self):
        for uid in range(1, 7):
            self.im.boxes["INBOX"][uid] = (raw_mail(f"Message {uid}"), "")
        self.db.set_setting("initial_fetch_limit", 2)
        self.assertEqual(protocols.sync_account(self.db, self.acc), 2)
        cancel = threading.Event()
        self.db.set_setting(f"history:{self.acc.id}", "running")

        def progress(text):
            if len(self.db.list_messages(self.acc.id, "inbox")) >= 4:
                cancel.set()

        with mock.patch.object(sync, "FETCH_BATCH", 2):
            protocols.sync_account(self.db, self.acc, progress, cancel=cancel)
        self.assertEqual(len(self.db.list_messages(self.acc.id, "inbox")), 4)
        self.assertEqual(protocols.sync_account(self.db, self.acc), 2)
        self.assertEqual(len(self.db.list_messages(self.acc.id, "inbox")), 6)
        self.assertEqual(self.db.get_setting(f"history:{self.acc.id}"), "complete")

    def test_partial_fetch_never_skips_missing_uid_on_retry(self):
        self.im.boxes["INBOX"] = {uid: (raw_mail(str(uid)), "") for uid in (1, 2, 3)}
        self.im.omit_uid = 2
        with self.assertRaises(Exception):
            protocols.sync_account(self.db, self.acc)
        self.im.omit_uid = None
        protocols.sync_account(self.db, self.acc)
        self.assertEqual(len(self.db.list_messages(self.acc.id, "inbox")), 3)


class TestGlobalSearch(Fixture):
    def test_cancelled_search_releases_progress_handler_for_later_database_operations(self):
        for number in range(80):
            self.add(raw_mail(str(number), "search text " * 20))
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(sqlite3.OperationalError):
            self.db.search_messages("missing", cancel=cancel)
        self.assertEqual(len(self.db.search_messages("search text")), 80)

    def test_deleting_one_reference_never_removes_another_messages_eml(self):
        raw = raw_mail("Shared file")
        first, second = self.add(raw), self.add(raw)
        self.assertEqual(self.db.message(first).raw_path, self.db.message(second).raw_path)
        self.db.delete_message(first)
        self.assertEqual(self.db.message(second).raw, raw)

    def test_unicode_body_and_attachment_names_with_account_folder_date_filters(self):
        first = self.add(raw_mail("Old", "ДОГОВОР готов", [("Счёт.xlsx", b"data")]))
        other = Account(email="other@example.com", storage_dir=str(self.root / "other"))
        self.db.save_account(other)
        m = parse_message(raw_mail("Other", "договор подписан")); m.account_id, m.folder = other.id, "sent"
        second = self.db.add_message(m)
        self.db.update_message(first, date_ts=100, seen=0)
        self.db.update_message(second, date_ts=200, seen=1)
        self.assertEqual({r["id"] for r in self.db.search_messages("Договор")}, {first, second})
        self.assertEqual([r["id"] for r in self.db.search_messages("сЧЁт.XLSX")], [first])
        self.assertEqual([r["id"] for r in self.db.search_messages("договор", account_id=other.id)], [second])
        self.assertEqual([r["id"] for r in self.db.search_messages("договор", since=150, until=201)], [second])
        self.assertEqual([r["id"] for r in self.db.search_messages("договор", unread=True)], [first])
        self.assertEqual(self.db.search_messages("' OR 1=1 --"), [])


class TestPersistentDelivery(Fixture):
    def queue(self):
        return self.db.save_composition(self.acc.id, raw_mail("Queued"), {"to": "me@example.com"}, queued=True)

    def test_offline_queue_survives_restart_then_sends_and_appends_once(self):
        mid = self.queue()
        with mock.patch.object(protocols, "smtp_connect", side_effect=ConnectionError("offline")):
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "queued")
        self.assertEqual(self.db.message(mid).delivery_state, "pending")
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            delivery.process_outbox(self.db, self.acc)
            delivery.process_outbox(self.db, self.acc)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertEqual(self.im.append_count, 1)
        self.assertEqual(self.db.message(mid).folder, "sent")
        self.assertEqual(self.db.message(mid).server_folder, "Sent Items")

    def test_smtp_success_with_lost_append_response_does_not_resend_or_duplicate_sent(self):
        mid = self.queue()
        smtp = mock.Mock(); smtp.send_message.return_value = {}
        self.im.lose_append_response = True
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            delivery.deliver_outbox(self.db, mid)
            self.assertEqual(self.db.message(mid).delivery_state, "append_pending")
            delivery.process_outbox(self.db, self.acc)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.assertEqual(self.im.append_count, 1)
        self.assertIsNone(self.db.message(mid).delivery_state)

    def test_uncertain_smtp_or_crash_is_not_automatically_retried(self):
        mid = self.queue()
        smtp = mock.Mock(); smtp.send_message.side_effect = ConnectionError("lost after DATA")
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp):
            self.assertEqual(delivery.deliver_outbox(self.db, mid), "uncertain")
            delivery.process_outbox(self.db, self.acc)
        self.assertEqual(smtp.send_message.call_count, 1)
        self.db.update_message(mid, delivery_state="sending")
        with mock.patch.object(protocols, "smtp_connect") as connect:
            delivery.process_outbox(self.db, self.acc)
            connect.assert_not_called()

    def test_provider_sent_copy_with_extra_headers_is_not_duplicated(self):
        mid = self.queue()
        raw = self.db.message(mid).raw
        self.im.boxes["Sent Items"][1] = (b"Received: by server\r\n" + raw, "\\Seen")
        self.db.update_message(mid, folder="sent", delivery_state="append_pending")
        delivery.append_sent(self.db, mid)
        self.assertEqual(self.im.append_count, 0)
        self.assertIsNone(self.db.message(mid).delivery_state)

    def test_atomic_queue_claim_prevents_two_database_clients_from_sending_twice(self):
        mid = self.queue()
        other = Database(self.db.path)
        barrier = threading.Barrier(2)
        results, errors = [], []
        smtp = mock.Mock(); smtp.send_message.return_value = {}

        def intercepted(db):
            original, calls = db.message, [0]
            def read(message_id):
                m = original(message_id)
                calls[0] += 1
                if calls[0] == 2:
                    barrier.wait(timeout=5)
                return m
            return read

        def worker(db):
            try:
                results.append(delivery.deliver_outbox(db, mid))
            except Exception as error:
                errors.append(error)
            finally:
                db.close()

        try:
            with mock.patch.object(self.db, "message", side_effect=intercepted(self.db)), \
                    mock.patch.object(other, "message", side_effect=intercepted(other)), \
                    mock.patch.object(protocols, "smtp_connect", return_value=smtp):
                threads = [threading.Thread(target=worker, args=(db,)) for db in (self.db, other)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(smtp.send_message.call_count, 1)
            self.assertIn("sent", results)
        finally:
            other.close()


class TestSingleInstance(Fixture):
    def test_same_data_directory_is_exclusive_but_other_directory_can_open(self):
        from mailclient.instance import acquire_instance
        leases = []
        try:
            first = acquire_instance(self.root / "one"); leases.append(first)
            self.assertIsNotNone(first)
            self.assertIsNone(acquire_instance(self.root / "one"))
            second = acquire_instance(self.root / "two"); leases.append(second)
            self.assertIsNotNone(second)
        finally:
            for lease in leases:
                if lease is None:
                    continue
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32")
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle(lease)
                else:
                    lease.close()


class TestCompleteUI(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(main_window, "run_task"),
                      mock.patch("mailclient.ui.mail_watch.MailWatcher"), mock.patch.object(viewer, "HAS_WEBENGINE", False)):
            patch.start(); self.addCleanup(patch.stop)
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.app.processEvents()

    def test_autosaved_draft_roundtrip_keeps_recipients_html_files_and_inline_images(self):
        dialog = ComposeDialog(self.db, account_id=self.acc.id, parent=self.window)
        dialog.to.setText("other@example.com")
        dialog.cc.setText("copy@example.com")
        dialog.subject.setText("Draft subject")
        dialog.editor.set_html("<p><b>Bold text</b></p>")
        image = QImage(2, 2, QImage.Format_ARGB32); image.fill(0xffffffff)
        buffer = QBuffer(); buffer.open(QIODevice.WriteOnly); image.save(buffer, "PNG")
        png = bytes(buffer.data())
        dialog.editor._insert_image_data(png, "image/png", "pixel.png")
        dialog.attachments.append(("draft.pdf", b"PDF")); dialog._refresh_attachments()
        dialog._save_draft()
        mid = dialog._draft_id
        dialog.close()
        reopened = ComposeDialog(self.db, draft_id=mid, parent=self.window)
        self.assertEqual(reopened.to.text(), "other@example.com")
        self.assertEqual(reopened.cc.text(), "copy@example.com")
        self.assertIn("Bold text", reopened.editor.to_plain())
        self.assertIn("font-weight:700", reopened.editor.to_html())
        self.assertEqual(reopened.attachments, [("draft.pdf", b"PDF")])
        self.assertEqual(len(reopened.editor.inline_images()), 1)
        cid = reopened.editor.inline_images()[0][0]
        self.assertFalse(reopened.editor.edit.document().resource(QTextDocument.ImageResource, QUrl("cid:"+cid)).isNull())
        reopened.close()

    def test_quick_reply_survives_selection_changes_and_reuses_draft_when_queued(self):
        first = self.add(raw_mail("First"))
        second = self.add(raw_mail("Second"))
        self.window._show_message(first)
        self.window.quick.edit.setPlainText("Unfinished reply")
        self.window._save_quick_draft()
        draft_id = self.window._quick_draft_id
        self.window._show_message(second)
        self.window._show_message(first)
        self.assertEqual(self.window.quick.edit.toPlainText(), "Unfinished reply")
        self.assertEqual(self.window._quick_draft_id, draft_id)
        self.window._quick_reply("Unfinished reply")
        self.assertEqual(self.db.message(draft_id).folder, "outbox")
        self.assertEqual(self.db.conn().execute("SELECT count(*) FROM messages WHERE folder='drafts'").fetchone()[0], 0)
        # Имитируем ответ фоновой отправки, не подключаясь к сети.
        main_window.run_task.call_args.kwargs["on_done"]("queued")
        self.assertEqual(self.window.quick.edit.toPlainText(), "")

    def test_background_global_results_keep_account_context_and_trash_confirmation(self):
        other = Account(email="other@example.com", storage_dir=str(self.root / "other")); self.db.save_account(other)
        m = parse_message(raw_mail("Global result", "unique needle")); m.account_id, m.folder = other.id, "trash"
        mid = self.db.add_message(m)
        self.window._global_search()
        self.window.search.setText("unique needle")
        self.window._run_search()
        call = main_window.run_task.call_args
        call.kwargs["on_done"](call.args[0](*call.args[1:]))
        self.assertEqual(self.window._selected_id(), mid)
        self.assertEqual(self.window.current[0], self.acc.id)
        self.assertFalse(self.window.table.isColumnHidden(main_window.COL["account"]))
        main_window.run_task.reset_mock()
        with mock.patch.object(main_window.QMessageBox, "question", return_value=main_window.QMessageBox.No) as question:
            self.window._delete()
        question.assert_called_once()
        main_window.run_task.assert_not_called()

    def test_stale_search_worker_cannot_replace_newer_query(self):
        first = self.add(raw_mail("First", "first needle"))
        second = self.add(raw_mail("Second", "second needle"))
        self.window._global_search()
        self.window.search.setText("first needle"); self.window._run_search()
        old = main_window.run_task.call_args
        self.window.search.setText("second needle"); self.window._run_search()
        new = main_window.run_task.call_args
        new.kwargs["on_done"](new.args[0](*new.args[1:]))
        old.kwargs["on_done"](old.args[0](*old.args[1:]))
        self.assertEqual(self.window._selected_id(), second)
        self.assertNotEqual(first, second)


class TestBackupRestore(Fixture):
    def test_full_backup_relocates_mail_and_preserves_local_files_and_queue_safety(self):
        raw = raw_mail(files=[("file.pdf", b"PDF")])
        mid = self.add(raw)
        manual = Path(self.acc.storage_dir) / "Вложения" / "manual.txt"
        manual.parent.mkdir(); manual.write_text("local edit", encoding="utf-8")
        queued = self.db.save_composition(self.acc.id, raw_mail("Pending"), {}, queued=True)
        archive = self.root / "backup.smbak"
        backup.create_backup(self.db, archive)
        self.db.update_message(mid, subject="Changed since backup")
        stage = Path(backup.prepare_restore(self.db, archive))
        self.assertEqual(self.db.message(mid).subject, "Changed since backup")
        self.assertTrue(json.loads((stage / "ready.json").read_text())["safety_backup"])
        data_dir = Path(self.db.path).parent
        self.db.close()
        backup.install_restore(stage, data_dir)
        restored = Database(data_dir / "mail.db")
        try:
            self.assertEqual(restored.message(mid).raw, raw)
            self.assertEqual(restored.message_attachments(mid), [("file.pdf", b"PDF")])
            self.assertEqual(restored.message(queued).delivery_state, "uncertain")
            new_root = Path(restored.account(self.acc.id).storage_dir)
            self.assertEqual((new_root / "Вложения" / "manual.txt").read_text(encoding="utf-8"), "local edit")
            self.assertTrue(Path(restored.message(mid).raw_path).is_relative_to(stage))
        finally:
            restored.close()

    def test_malicious_archive_is_rejected_without_changing_working_mail(self):
        mid = self.add(raw_mail())
        for name in ("../escape.txt", "/absolute.txt", "C:/escape.txt", "bad\\path"):
            with self.subTest(name=name):
                archive = self.root / "bad.smbak"
                with zipfile.ZipFile(archive, "w") as zip_file:
                    zip_file.writestr(name, "bad")
                with self.assertRaises(ValueError):
                    backup.prepare_restore(self.db, archive)
                self.assertIsNotNone(self.db.message(mid))


if __name__ == "__main__":
    unittest.main()
