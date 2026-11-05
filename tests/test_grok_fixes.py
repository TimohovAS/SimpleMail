"""Regressions found in Grok's review; all data and credentials are test fixtures."""
import base64
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QUrl, QBuffer, QIODevice
from PySide6.QtGui import QTextDocument, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient import backup, passwords, protocols
from mailclient.db import Account, Database
from mailclient.mailparse import parse_message
from mailclient.ui import account_dialog, attachments_bar, viewer
from test_complete_features import FolderIMAP


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "data" / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = Account(name="Last, First", email="a@example.com", storage_dir=str(self.root / "mail"))
        self.db.save_account(self.acc)

    def add_mail(self, account=None, subject="Test"):
        account = account or self.acc
        raw = protocols.build_message(account, "b@example.com", subject, "body").as_bytes()
        message = parse_message(raw)
        message.account_id = account.id
        return self.db.add_message(message)


class TestPasswordSource(Fixture):
    def test_failed_keyring_save_uses_new_fallback_including_empty_password(self):
        ring = mock.Mock()
        ring.get_password.return_value = "obsolete-test-password"
        ring.set_password.side_effect = RuntimeError("unavailable")
        with mock.patch.object(passwords, "keyring", ring):
            for password in ("new-test-password", ""):
                self.assertFalse(passwords.set_password(self.db, self.acc.id, password))
                self.assertEqual(passwords.get_password(self.db, self.acc.id), password)
            ring.get_password.assert_not_called()

    def test_legacy_fallback_overrides_stale_keyring_and_success_switches_back(self):
        self.db.set_setting(f"pw:{self.acc.id}", "fallback-test-password")
        ring = mock.Mock()
        ring.get_password.return_value = "keyring-test-password"
        with mock.patch.object(passwords, "keyring", ring):
            self.assertEqual(passwords.get_password(self.db, self.acc.id), "fallback-test-password")
            self.assertTrue(passwords.set_password(self.db, self.acc.id, "keyring-test-password"))
            self.assertEqual(passwords.get_password(self.db, self.acc.id), "keyring-test-password")
        self.assertEqual(self.db.get_setting(f"pw:{self.acc.id}"), "")

    def test_failed_keyring_delete_never_resurrects_old_password(self):
        ring = mock.Mock()
        ring.delete_password.side_effect = RuntimeError("unavailable")
        ring.get_password.return_value = "obsolete-test-password"
        with mock.patch.object(passwords, "keyring", ring):
            passwords.delete_password(self.db, self.acc.id)
            self.assertEqual(passwords.get_password(self.db, self.acc.id), "")


class TestHeaders(Fixture):
    def test_special_and_unicode_names_roundtrip_as_one_address(self):
        for name in ("Last, First", 'Andrey "Boss"', "Иванов, Андрей"):
            with self.subTest(name=name):
                self.acc.name = name
                message = protocols.build_message(self.acc, "b@example.com", "Test", "body")
                header = BytesParser(policy=policy.default).parsebytes(message.as_bytes())["From"]
                self.assertEqual(len(header.addresses), 1)
                self.assertEqual(header.addresses[0].display_name, name)
                self.assertEqual(header.addresses[0].addr_spec, self.acc.email)

    def test_redirect_has_valid_headers_and_preserves_original_bytes(self):
        raw = b"From: original@example.com\r\nSubject: Original\r\n\r\noriginal body\r\n"
        smtp = mock.Mock()
        with mock.patch.object(protocols, "smtp_connect", return_value=smtp), \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.redirect_message(self.db, self.acc, raw, '"Other, Person" <b@example.com>')
        sender, recipients, sent = smtp.sendmail.call_args.args
        self.assertEqual(recipients, ["b@example.com"])
        self.assertTrue(sent.endswith(raw))
        parsed = BytesParser(policy=policy.default).parsebytes(sent)
        self.assertEqual(parsed["Resent-From"].addresses[0].display_name, self.acc.name)
        self.assertEqual(parsed["Resent-To"].addresses[0].display_name, "Other, Person")
        self.assertEqual(str(parsed["From"]), "original@example.com")

    def test_injected_headers_are_rejected_before_connecting(self):
        with mock.patch.object(protocols, "smtp_connect") as connect:
            for field in ("name", "email"):
                account = Account(name="Test", email="a@example.com")
                setattr(account, field, "a@example.com\r\nX-Injected: yes")
                with self.assertRaises(ValueError):
                    protocols.redirect_message(self.db, account, b"Subject: Test\r\n\r\nbody", "b@example.com")
            with self.assertRaises(ValueError):
                protocols.redirect_message(self.db, self.acc, b"Subject: Test\r\n\r\nbody", "b@example.com\r\nX-Injected: yes")
            connect.assert_not_called()


class TestAccountRemoval(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_ui_requires_explicit_choice_to_remove_files_and_runs_in_background(self):
        for remove_files in (False, True):
            with self.subTest(remove_files=remove_files):
                dialog = account_dialog.AccountsDialog(self.db)
                dialog.list.setCurrentRow(0)
                def confirm(box):
                    self.assertFalse(box.checkBox().isChecked())
                    self.assertEqual(box.defaultButton(), box.button(QMessageBox.No))
                    box.checkBox().setChecked(remove_files)
                    return QMessageBox.Yes
                try:
                    with mock.patch.object(account_dialog.QMessageBox, "exec", confirm), \
                            mock.patch.object(account_dialog, "run_task") as task, \
                            mock.patch.object(self.db, "delete_account", return_value=[]) as delete, \
                            mock.patch.object(account_dialog, "delete_password") as forget:
                        dialog._delete()
                        self.assertFalse(dialog.isEnabled())
                        delete.assert_not_called()
                        remove = task.call_args.args[0]
                        result = remove()
                        delete.assert_called_once_with(self.acc.id, delete_files=remove_files)
                        forget.assert_called_once_with(self.db, self.acc.id, forget=True)
                        task.call_args.kwargs["on_done"](result)
                        self.assertTrue(dialog.isEnabled())
                finally:
                    dialog.close()

    def test_default_keeps_exports_and_cleans_account_settings(self):
        mid = self.add_mail()
        path = Path(self.db.message(mid).raw_path)
        for prefix in ("pw", "pw_source", "history"):
            self.db.set_setting(f"{prefix}:{self.acc.id}", "test")
        self.assertEqual(self.db.delete_account(self.acc.id), [])
        self.assertTrue(path.is_file())
        self.assertIsNone(self.db.account(self.acc.id))
        for prefix in ("pw", "pw_source", "history"):
            self.assertIsNone(self.db.get_setting(f"{prefix}:{self.acc.id}"))

    def test_explicit_cleanup_removes_only_tracked_eml(self):
        mid = self.add_mail()
        path = Path(self.db.message(mid).raw_path)
        manual = Path(self.acc.storage_dir) / "manual.pdf"
        manual.write_bytes(b"keep")
        unrelated = path.with_name("unrelated.eml")
        unrelated.write_bytes(b"keep")
        self.assertEqual(self.db.delete_account(self.acc.id, delete_files=True), [])
        self.assertFalse(path.exists())
        self.assertEqual(manual.read_bytes(), b"keep")
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_shared_file_and_path_outside_mailbox_are_preserved(self):
        mid = self.add_mail()
        shared = Path(self.db.message(mid).raw_path)
        other = Account(email="other@example.com", storage_dir=self.acc.storage_dir)
        self.db.save_account(other)
        other_mid = self.add_mail(other)
        self.db.update_message(other_mid, raw_path=str(shared))
        outside = self.root / "outside.eml"
        outside.write_bytes(b"outside")
        outside_mid = self.add_mail(subject="Outside")
        self.db.update_message(outside_mid, raw_path=str(outside))
        self.db.delete_account(self.acc.id, delete_files=True)
        self.assertTrue(shared.exists())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertIsNotNone(self.db.account(other.id))

    def test_failed_file_removal_is_reported(self):
        mid = self.add_mail()
        path = Path(self.db.message(mid).raw_path)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            self.assertEqual(self.db.delete_account(self.acc.id, delete_files=True), [str(path)])
        self.assertTrue(path.exists())


class TestServerSpamMove(Fixture):
    def test_phone_move_clears_local_spam_regardless_of_folder_order_or_message_id(self):
        class Boxes(FolderIMAP):
            def list(self, *args):
                return "OK", [b'(\\Junk) "/" "Junk"' if name == "Junk" else b'() "/" "INBOX"' for name in self.boxes]
        for first in ("Junk", "INBOX"):
            for has_id in (True, False):
                with self.subTest(first=first, has_id=has_id):
                    for row in self.db.conn().execute("SELECT id FROM messages").fetchall():
                        self.db.delete_message(row[0])
                    with self.db.conn() as c:
                        c.execute("DELETE FROM folders WHERE account_id=?", (self.acc.id,))
                    em = protocols.build_message(self.acc, "b@example.com", f"Move {first} {has_id}", "body")
                    if not has_id:
                        del em["Message-ID"]
                    raw = em.as_bytes()
                    order = (first, "INBOX" if first == "Junk" else "Junk")
                    fake = Boxes({name: ({5: (raw, "")} if name == "Junk" else {}) for name in order})
                    with mock.patch.object(protocols, "imap_connect", return_value=fake), \
                            mock.patch.object(protocols, "get_password", return_value="test"):
                        protocols.sync_account(self.db, self.acc)
                        mid = self.db.conn().execute("SELECT id FROM messages WHERE folder='spam'").fetchone()[0]
                        self.db.update_message(mid, is_spam=1)
                        fake.boxes["INBOX"][9] = fake.boxes["Junk"].pop(5)
                        protocols.sync_account(self.db, self.acc)
                        protocols.sync_account(self.db, self.acc)
                    inbox = self.db.list_messages(self.acc.id, "inbox")
                    self.assertEqual(len(inbox), 1)
                    self.assertFalse(inbox[0]["is_spam"])


class TestArchiveLimits(Fixture):
    def test_backup_excludes_token_cache_and_preserves_explicit_empty_password_on_restore(self):
        self.add_mail()
        cache = Path(self.acc.storage_dir) / "OAuth"
        cache.mkdir()
        (cache / "cache.bin").write_bytes(b"test-encrypted-token-cache")
        self.db.set_setting(f"pw:{self.acc.id}", "")
        self.db.set_setting(f"pw_source:{self.acc.id}", "database")
        archive = self.root / "backup.smbak"
        with mock.patch("mailclient.oauth.cache_root", return_value=cache):
            backup.create_backup(self.db, archive)
            with zipfile.ZipFile(archive) as copied:
                self.assertFalse(any("OAuth" in name for name in copied.namelist()))
                snapshot = self.root / "snapshot.db"
                snapshot.write_bytes(copied.read("mail.db"))
            with closing(sqlite3.connect(snapshot)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM settings WHERE key LIKE 'pw:%' OR key LIKE 'pw_source:%'").fetchone()[0], 0)
            prepared = Path(backup.prepare_restore(self.db, archive))
        with closing(sqlite3.connect(prepared / "mail.db")) as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key=?", (f"pw:{self.acc.id}",)).fetchone()[0], "")
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key=?", (f"pw_source:{self.acc.id}",)).fetchone()[0], "database")

    def archive(self, entries):
        path = self.root / "bad.smbak"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return path

    def test_size_and_count_limits_reject_before_extracting(self):
        for setting, maximum in (("MAX_ARCHIVE_ENTRIES", 1), ("MAX_EXTRACTED_BYTES", 5)):
            with self.subTest(setting=setting):
                archive = self.archive([("large", b"x" * 1000), ("another", b"x")])
                stage = self.root / setting
                stage.mkdir()
                with mock.patch.object(backup, setting, maximum), self.assertRaises(ValueError):
                    backup._extract_archive(archive, stage)
                self.assertEqual(list(stage.iterdir()), [])

    def test_low_disk_space_rejects_before_extracting(self):
        archive = self.archive([("data", b"test")])
        stage = self.root / "stage"
        stage.mkdir()
        with mock.patch.object(backup.shutil, "disk_usage", return_value=SimpleNamespace(free=1)), self.assertRaises(ValueError):
            backup._extract_archive(archive, stage)
        self.assertEqual(list(stage.iterdir()), [])

    def test_windows_aliases_and_devices_are_rejected(self):
        for names in (("mail.db", "MAIL.DB"), ("CON.txt",), ("mail.db.",), ("../escape",)):
            with self.subTest(names=names):
                stage = self.root / "stage"
                stage.mkdir(exist_ok=True)
                archive = self.archive([(name, b"test") for name in names])
                with self.assertRaises(ValueError):
                    backup._extract_archive(archive, stage)
                self.assertEqual(list(stage.iterdir()), [])

    def test_rejected_restore_keeps_current_database(self):
        mid = self.add_mail()
        original = self.db.message(mid).raw
        archive = self.archive([("huge", b"x" * 1000)])
        with mock.patch.object(backup, "MAX_EXTRACTED_BYTES", 5), self.assertRaises(ValueError):
            backup.prepare_restore(self.db, archive)
        self.assertEqual(self.db.message(mid).raw, original)


class TestSafeViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_remote_detector_covers_resources_and_ignores_ordinary_links(self):
        for body in ('<base href="https://example.com/">', '<link href="//example.com/a.css">',
                     '<object data="https://example.com/a">', '<style>@import "https://example.com/a";</style>',
                     '<img srcset="cid:logo 1x, https://example.com/a 2x">',
                     '<svg><use xlink:href="https://example.com/a"/></svg>'):
            with self.subTest(body=body):
                self.assertTrue(viewer.has_remote_content(body))
        self.assertFalse(viewer.has_remote_content('<a href="https://example.com">Link</a><img src="cid:logo">'))

    def test_active_markup_and_base_are_removed(self):
        result = viewer.wrap_html('<base href="https://example.com"><script>bad()</script><iframe src="https://example.com">bad</iframe><p>Keep</p>')
        self.assertNotIn("<base", result)
        self.assertNotIn("<script", result)
        self.assertNotIn("<iframe", result)
        self.assertNotIn("bad()", result)
        self.assertIn("<p>Keep</p>", result)

    def test_fallback_blocks_files_and_network_but_keeps_inline_images(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            view = viewer.MessageView()
        try:
            for url in ("file:///C:/secret.png", "file://server/share/a.png", "https://example.com/a.png", "qrc:/a.png"):
                self.assertIsNone(view.text.loadResource(QTextDocument.ImageResource, QUrl(url)))
            image = QImage(2, 2, QImage.Format_RGB32); image.fill(0)
            buffer = QBuffer(); buffer.open(QIODevice.WriteOnly); image.save(buffer, "PNG")
            png = bytes(buffer.data())
            view._inline = {"logo": ("image/png", png), "fake": ("image/png", b'<svg xmlns="http://www.w3.org/2000/svg"/>')}
            self.assertEqual(view.text.loadResource(QTextDocument.ImageResource, QUrl("cid:logo")).size(), image.size())
            self.assertIsNone(view.text.loadResource(QTextDocument.ImageResource, QUrl("cid:fake")))
            data = base64.b64encode(png).decode()
            self.assertEqual(view.text.loadResource(QTextDocument.ImageResource, QUrl("data:image/png;base64," + data)).size(), image.size())
            self.assertIsNone(view.text.loadResource(QTextDocument.ImageResource, QUrl("data:image/svg+xml,<svg/>")))
        finally:
            view.close()


class TestTemporaryAttachments(unittest.TestCase):
    def test_same_names_never_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tempfile, "tempdir", directory):
            first = attachments_bar.temporary_attachment("same.txt", b"first")
            second = attachments_bar.temporary_attachment("same.txt", b"second")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")

    def test_active_document_extensions_require_warning(self):
        for name in ("mail.html", "mail.HTM", "image.svg", "image.svgz", "x.xhtml"):
            self.assertTrue(attachments_bar.is_dangerous(name))
