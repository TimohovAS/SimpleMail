"""OAuth2, MIME quotes, exact spam rules and server compatibility without external mail."""
import base64
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from mailclient import oauth, passwords, protocols, sync
from mailclient.db import Account, Message
from mailclient.mailparse import parse_message
from mailclient.spam import SpamFilter
from mailclient.ui.account_dialog import AccountEditDialog, PRESETS
from mailclient.ui.compose_dialog import ComposeDialog
from test_grok_fixes import Fixture
from test_complete_features import FolderIMAP


class TestOAuth(Fixture):
    def setUp(self):
        super().setUp()
        oauth._contexts.clear()
        self.addCleanup(oauth._contexts.clear)
        self.acc.auth_method = "microsoft"
        self.acc.oauth_client_id = "11111111-2222-3333-4444-555555555555"
        self.acc.username = self.acc.email
        self.acc.imap_host = "outlook.office365.com"
        self.acc.smtp_host = "smtp.office365.com"
        self.db.save_account(self.acc)

    def test_msal_uses_encrypted_cache_or_memory_never_plaintext_fallback(self):
        import msal
        import msal_extensions
        app = mock.Mock()
        app.get_accounts.return_value = [{"username": self.acc.email}]
        app.acquire_token_silent.return_value = {"access_token": "test-access-token"}
        app.initiate_device_flow.return_value = {"user_code": "ABCD-EFGH", "device_code": "test-device-code",
                                                "verification_uri": "https://microsoft.com/devicelogin"}
        app.acquire_token_by_device_flow.return_value = {"access_token": "test-access-token"}
        with mock.patch.object(oauth, "_cache_location", return_value=self.root / "oauth" / "cache.bin"), \
                mock.patch.object(msal, "PublicClientApplication", return_value=app) as create, \
                mock.patch.object(msal_extensions, "build_encrypted_persistence", side_effect=RuntimeError("unavailable")):
            self.assertEqual(passwords.get_password(self.db, self.acc.id), "test-access-token")
            self.assertIsInstance(create.call_args.kwargs["token_cache"], msal.SerializableTokenCache)
            flow = oauth.begin_sign_in(self.db, self.acc)
            self.assertFalse(oauth.finish_sign_in(self.db, self.acc, flow, threading.Event()))
            cancel = threading.Event(); cancel.set()
            self.assertIsNone(oauth.finish_sign_in(self.db, self.acc, flow, cancel))
            app.acquire_token_by_device_flow.assert_called_once()
        self.assertIsNone(self.db.get_setting(f"pw:{self.acc.id}"))

    def test_invalid_client_id_and_plaintext_connections_are_rejected(self):
        for field, value in (("oauth_client_id", ""), ("oauth_tenant", "https://evil.example"),
                             ("imap_security", "none"), ("smtp_security", "none"), ("imap_host", "evil.example")):
            with self.subTest(field=field):
                old = getattr(self.acc, field)
                setattr(self.acc, field, value)
                with self.assertRaises(oauth.OAuthError):
                    oauth.validate_config(self.acc)
                setattr(self.acc, field, old)

    def test_sasl_and_both_protocols_use_xoauth2(self):
        payload = oauth.sasl_payload(self.acc.email, "test-token")
        im = mock.Mock(capabilities=("IMAP4rev1",))
        with mock.patch.object(protocols.imaplib, "IMAP4_SSL", return_value=im):
            protocols.imap_connect(self.acc, "test-token")
        self.assertEqual(im.authenticate.call_args.args[0], "XOAUTH2")
        respond = im.authenticate.call_args.args[1]
        self.assertEqual(respond(b""), payload)
        self.assertEqual(respond(b"error"), b"")
        im.login.assert_not_called()
        smtp = mock.Mock()
        smtp.docmd.return_value = (235, b"OK")
        with mock.patch.object(protocols.smtplib, "SMTP_SSL", return_value=smtp):
            protocols.smtp_connect(self.acc, "test-token")
        encoded = smtp.docmd.call_args.args[1].split()[1]
        self.assertEqual(base64.b64decode(encoded), payload)
        smtp.login.assert_not_called()
        with self.assertRaises(oauth.OAuthError):
            oauth.sasl_payload("a@example.com\x01auth=bad", "token")

    def test_expired_session_requires_explicit_sign_in(self):
        app = mock.Mock()
        app.get_accounts.return_value = []
        with mock.patch.object(oauth, "_context", return_value=(app, threading.RLock(), False)):
            with self.assertRaises(oauth.OAuthError):
                oauth.access_token(self.db, self.acc)
        app.acquire_token_silent.assert_not_called()

    def test_old_account_snapshot_cannot_receive_new_oauth_token(self):
        from dataclasses import replace
        old = replace(self.acc, auth_method="password")
        with mock.patch.object(oauth, "access_token") as token:
            with self.assertRaises(ValueError):
                passwords.get_password(self.db, self.acc.id, account=old)
            token.assert_not_called()

    def test_deleted_account_does_not_retrieve_stale_credentials(self):
        self.db.delete_account(self.acc.id)
        with mock.patch.object(passwords, "keyring") as ring:
            with self.assertRaises(ValueError):
                passwords.get_password(self.db, self.acc.id, account=self.acc)
            ring.get_password.assert_not_called()

    def test_device_login_rejects_non_microsoft_verification_page(self):
        app = mock.Mock()
        app.initiate_device_flow.return_value = {"user_code": "TEST", "verification_uri": "https://evil.example/login"}
        with mock.patch.object(oauth, "_context", return_value=(app, threading.RLock(), True)):
            with self.assertRaises(oauth.OAuthError):
                oauth.begin_sign_in(self.db, self.acc)

    def test_dialog_cancellation_expires_flow_and_ignores_late_success(self):
        from mailclient.ui import oauth_dialog
        from PySide6.QtWidgets import QDialog
        app = QApplication.instance() or QApplication([])
        with mock.patch.object(oauth_dialog, "run_task") as task:
            dialog = oauth_dialog.OAuthDialog(self.db, self.acc)
            ready = task.call_args.kwargs["on_done"]
            flow = {"user_code": "TEST", "verification_uri": "https://microsoft.com/devicelogin", "expires_at": 100}
            ready(flow)
            finished = task.call_args.kwargs["on_done"]
            dialog.reject()
            finished(True)
            self.assertTrue(dialog.cancel.is_set())
            self.assertEqual(flow["expires_at"], 0)
            self.assertEqual(dialog.result(), QDialog.Rejected)
            self.assertFalse(dialog.persistent)
            dialog.close()


class TestProtocolCompatibility(unittest.TestCase):
    def test_folder_identity_survives_utf8_enable(self):
        for name in ("INBOX", "Проекты", "Sent & Drafts", "Koš"):
            with self.subTest(name=name):
                key = protocols.encode_mailbox(name)
                self.assertEqual(protocols.decode_mailbox(key), name)
                self.assertEqual(protocols.imap_mailbox(key, mock.Mock(utf8_enabled=True)), protocols.imap_quote(name))
                self.assertTrue(protocols.imap_mailbox(key).isascii())
        legacy = mock.Mock(utf8_enabled=False)
        modern = mock.Mock(utf8_enabled=True)
        key = protocols.encode_mailbox("Папка")
        legacy.list.return_value = ("OK", [b'() "/" "' + key.encode() + b'"'])
        modern.list.return_value = ("OK", ['() "/" "Папка"'.encode()])
        self.assertEqual(sync.discover_folders(legacy)[-1].name, sync.discover_folders(modern)[-1].name)

    def test_utf8_smtp_login_and_mechanism_fallback(self):
        smtp = mock.Mock(esmtp_features={"auth": "LOGIN"})
        smtp.docmd.side_effect = [(334, b"username"), (334, b"password"), (235, b"OK")]
        protocols.smtp_login(smtp, "пользователь", "пароль")
        calls = smtp.docmd.call_args_list
        self.assertEqual(calls[0].args, ("AUTH", "LOGIN"))
        self.assertEqual(base64.b64decode(calls[1].args[0]).decode(), "пользователь")
        self.assertEqual(base64.b64decode(calls[2].args[0]).decode(), "пароль")
        smtp = mock.Mock(esmtp_features={"auth": "PLAIN LOGIN"})
        smtp.docmd.side_effect = [(504, b"unsupported"), (334, b"u"), (334, b"p"), (235, b"OK")]
        protocols.smtp_login(smtp, "user", "пароль")
        self.assertEqual(smtp.docmd.call_count, 4)


class TestExactRules(Fixture):
    def test_address_matches_real_address_not_display_name_or_substring(self):
        self.db.add_spam_rule("black", "address", "trusted@example.com")
        filter = SpamFilter(self.db)
        self.assertTrue(filter.classify(Message(sender='"Real, Name" <trusted@example.com>')).is_spam)
        self.assertFalse(filter.classify(Message(sender='"trusted@example.com" <attacker@evil.example>')).is_spam)
        self.assertFalse(filter.classify(Message(sender="nottrusted@example.com")).is_spam)

    def test_domain_is_exact_and_white_rule_wins(self):
        self.db.add_spam_rule("black", "domain", "example.com")
        self.db.add_spam_rule("white", "address", "friend@example.com")
        filter = SpamFilter(self.db)
        self.assertTrue(filter.classify(Message(sender="other@example.com")).is_spam)
        self.assertFalse(filter.classify(Message(sender="friend@example.com")).is_spam)
        self.assertFalse(filter.classify(Message(sender="a@sub.example.com")).is_spam)
        self.assertFalse(filter.classify(Message(sender="a@example.com.evil")).is_spam)


class TestRichQuotesAndSettings(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_html_quote_keeps_cid_and_formatting_through_draft(self):
        image = QImage(2, 2, QImage.Format_RGB32)
        image.fill(0xff0000)
        buffer = QBuffer(); buffer.open(QIODevice.WriteOnly); image.save(buffer, "PNG")
        png = bytes(buffer.data())
        dialog = ComposeDialog(self.db, account_id=self.acc.id, to="b@example.com", quote_header="Original",
                               quote_body_html='<p><b>Bold quote</b><img src="cid:logo"><img src="https://tracker.example/a"></p>',
                               quote_images=[("logo", "image/png", png)])
        try:
            state, message = dialog._composition()
            self.assertIn("font-weight:700", state["html"])
            self.assertNotIn("tracker.example", state["html"])
            self.assertEqual(dialog.editor.inline_images(), [("logo", "image/png", png)])
            self.assertEqual(len([part for part in message.walk() if part.get("Content-ID") == "<logo>"]), 1)
            dialog._dirty = True
            dialog._save_draft()
            mid = dialog._draft_id
        finally:
            dialog.close()
        reopened = ComposeDialog(self.db, draft_id=mid)
        try:
            self.assertEqual(reopened.editor.inline_images(), [("logo", "image/png", png)])
        finally:
            reopened.close()

    def test_custom_ports_survive_security_changes_and_aliases_exist(self):
        dialog = AccountEditDialog(self.db, self.acc)
        try:
            dialog.imap_port.setValue(1993)
            dialog.smtp_port.setValue(2525)
            dialog.imap_sec.setCurrentIndex(dialog.imap_sec.findData("starttls"))
            dialog.smtp_sec.setCurrentIndex(dialog.smtp_sec.findData("starttls"))
            self.assertEqual(dialog.imap_port.value(), 1993)
            self.assertEqual(dialog.smtp_port.value(), 2525)
            for domain in ("yandex.com", "yandex.by", "googlemail.com", "live.com", "msn.com"):
                self.assertIn(domain, PRESETS)
        finally:
            dialog.close()


class TestAutomaticServerJunk(Fixture):
    def test_automatic_spam_uses_existing_connection_and_moves_to_junk_during_idle_check(self):
        class Boxes(FolderIMAP):
            capabilities = ("IMAP4rev1", "MOVE", "UIDPLUS")
            def capability(self):
                return "OK", [b"IMAP4rev1 MOVE UIDPLUS"]
            def list(self, *args):
                return "OK", [b'() "/" "INBOX"', b'(\\Junk) "/" "Junk"']
            def response(self, code):
                return (code, [self.mapping]) if code == "COPYUID" else super().response(code)
            def uid(self, command, *args):
                if command == "MOVE":
                    target = args[1].decode().strip('"')
                    uid = int(args[0])
                    new_uid = max(self.boxes[target], default=0) + 1
                    self.boxes[target][new_uid] = self.boxes[self.selected].pop(uid)
                    self.mapping = f"{self.validity[target]} {uid} {new_uid}".encode()
                    return "OK", [b"moved"]
                return super().uid(command, *args)
        em = protocols.build_message(self.acc, "b@example.com", "Spam", "body")
        em["X-Spam-Flag"] = "YES"
        fake = Boxes({"INBOX": {1: (em.as_bytes(), "")}, "Junk": {}})
        with mock.patch.object(protocols, "imap_connect", return_value=fake) as connect, \
                mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc, inbox_only=True)
        connect.assert_called_once()
        self.assertEqual(fake.boxes["INBOX"], {})
        self.assertEqual(len(fake.boxes["Junk"]), 1)
        row = self.db.conn().execute("SELECT folder,server_folder FROM messages").fetchone()
        self.assertEqual(tuple(row), ("spam", "Junk"))

    def test_automatic_spam_stays_local_without_safe_move_support(self):
        class Boxes(FolderIMAP):
            capabilities = ("IMAP4rev1",)
            def capability(self):
                return "OK", [b"IMAP4rev1"]
            def list(self, *args):
                return "OK", [b'() "/" "INBOX"', b'(\Junk) "/" "Junk"']
        em = protocols.build_message(self.acc, "b@example.com", "Spam", "body")
        em["X-Spam-Flag"] = "YES"
        fake = Boxes({"INBOX": {1: (em.as_bytes(), "")}, "Junk": {}})
        with mock.patch.object(protocols, "imap_connect", return_value=fake),                 mock.patch.object(protocols, "get_password", return_value="test"):
            protocols.sync_account(self.db, self.acc, inbox_only=True)
        self.assertEqual(len(fake.boxes["INBOX"]), 1)
        row = self.db.conn().execute("SELECT folder,is_spam FROM messages").fetchone()
        self.assertEqual(tuple(row), ("inbox", 1))
