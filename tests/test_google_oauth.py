"""Gmail: вход через Google, понятная ошибка «нужен пароль приложения», честная ошибка SMTP."""
import base64
import imaplib
import json
import smtplib
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from PySide6.QtWidgets import QApplication

from mailclient import errors, google_oauth, oauth, passwords, protocols
from test_grok_fixes import Fixture


def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class GoogleFixture(Fixture):
    def setUp(self):
        super().setUp()
        google_oauth._memory.clear()
        self.addCleanup(google_oauth._memory.clear)
        patcher = mock.patch.object(oauth, "cache_root", return_value=self.root / "oauth")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.acc.email = self.acc.username = "user@gmail.com"
        self.acc.auth_method = "google"
        self.acc.oauth_client_id = "123-test.apps.googleusercontent.com"
        self.acc.oauth_client_secret = "test-secret"
        self.acc.imap_host, self.acc.smtp_host = "imap.gmail.com", "smtp.gmail.com"
        self.acc.smtp_port, self.acc.smtp_security = 465, "ssl"
        self.db.save_account(self.acc)

    def sign_in(self, email="user@gmail.com", state=None):
        flow = oauth.begin_sign_in(self.db, self.acc)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(flow["auth_url"]).query)
        self.assertEqual(query["redirect_uri"][0], flow["redirect"])
        self.assertEqual(query["code_challenge_method"][0], "S256")
        self.assertIn("https://mail.google.com/", query["scope"][0])

        def browser():
            params = urllib.parse.urlencode({"code": "test-code", "state": state or query["state"][0]})
            urllib.request.urlopen(f"{flow['redirect']}/?{params}", timeout=5).read()
        threading.Thread(target=browser, daemon=True).start()
        tokens = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
                  "id_token": id_token(email)}
        with mock.patch.object(google_oauth, "_post", return_value=tokens) as post:
            result = oauth.finish_sign_in(self.db, self.acc, flow, threading.Event())
        return result, post


class TestGoogleSignIn(GoogleFixture):
    def test_browser_redirect_pkce_and_token_refresh(self):
        result, post = self.sign_in()
        self.assertIsInstance(result, bool)
        sent = post.call_args.args[1]
        self.assertEqual(sent["code"], "test-code")
        self.assertEqual(sent["client_secret"], "test-secret")
        self.assertTrue(sent["code_verifier"])
        self.assertEqual(passwords.get_password(self.db, self.acc.id), "access-1")
        # Истёкший токен обновляется через refresh_token.
        key = google_oauth._key(self.db, self.acc)
        tokens = google_oauth._load(key)
        tokens["expires_at"] = time.time() - 1
        google_oauth._save(key, tokens)
        with mock.patch.object(google_oauth, "_post", return_value={"access_token": "access-2", "expires_in": 3600}) as post:
            self.assertEqual(oauth.access_token(self.db, self.acc), "access-2")
        self.assertEqual(post.call_args.args[1]["grant_type"], "refresh_token")
        self.assertEqual(post.call_args.args[1]["refresh_token"], "refresh-1")

    def test_revoked_session_requires_new_sign_in(self):
        self.sign_in()
        key = google_oauth._key(self.db, self.acc)
        tokens = google_oauth._load(key)
        tokens["expires_at"] = 0
        google_oauth._save(key, tokens)
        with mock.patch.object(google_oauth, "_post", return_value={"error": "invalid_grant"}):
            with self.assertRaises(google_oauth.GoogleAuthError):
                oauth.access_token(self.db, self.acc)
        with self.assertRaises(google_oauth.GoogleAuthError):
            oauth.access_token(self.db, self.acc)

    def test_other_google_account_and_forged_state_are_rejected(self):
        with self.assertRaises(google_oauth.GoogleAuthError):
            self.sign_in(email="other@gmail.com")
        with self.assertRaises(google_oauth.GoogleAuthError):
            self.sign_in(state="forged")
        with self.assertRaises(google_oauth.GoogleAuthError):
            oauth.access_token(self.db, self.acc)

    def test_cancel_stops_waiting_and_logout_removes_tokens(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        cancel = threading.Event(); cancel.set()
        self.assertIsNone(oauth.finish_sign_in(self.db, self.acc, flow, cancel))
        self.sign_in()
        with mock.patch.object(google_oauth, "_post", return_value={}) as revoke:
            oauth.logout(self.db, self.acc)
        self.assertEqual(revoke.call_args.args[0], google_oauth.REVOKE_URL)
        with self.assertRaises(google_oauth.GoogleAuthError):
            oauth.access_token(self.db, self.acc)

    def test_config_validation_and_xoauth2(self):
        for field, value in (("oauth_client_id", "not-a-google-id"), ("oauth_client_secret", ""),
                             ("imap_host", "imap.example.com"), ("smtp_security", "none")):
            with self.subTest(field=field):
                old = getattr(self.acc, field)
                setattr(self.acc, field, value)
                with self.assertRaises(google_oauth.GoogleAuthError):
                    oauth.validate_config(self.acc)
                setattr(self.acc, field, old)
        im = mock.Mock(capabilities=("IMAP4rev1",))
        with mock.patch.object(protocols.imaplib, "IMAP4_SSL", return_value=im):
            protocols.imap_connect(self.acc, "test-token")
        self.assertEqual(im.authenticate.call_args.args[0], "XOAUTH2")
        im.login.assert_not_called()

    def test_builtin_client_is_optional(self):
        self.assertEqual(oauth.default_client("google").keys(), {"client_id", "client_secret"})


class TestGmailErrors(unittest.TestCase):
    def test_app_password_required_is_explained(self):
        for error in (imaplib.IMAP4.error(b"[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833 (Failure)"),
                      smtplib.SMTPAuthenticationError(534, b"5.7.9 Application-specific password required.")):
            with self.subTest(error=type(error).__name__):
                info = errors.explain(error)
                self.assertIn("Google", info.summary)
                self.assertIn(errors.GOOGLE_APP_PASSWORDS, info.hint)

    def test_failed_password_is_not_hidden_behind_disconnect(self):
        smtp = mock.Mock(esmtp_features={"auth": " LOGIN PLAIN XOAUTH2"})
        smtp.docmd.side_effect = [(534, b"5.7.9 Application-specific password required"),
                                  smtplib.SMTPServerDisconnected("closed")]
        with self.assertRaises(smtplib.SMTPAuthenticationError) as raised:
            protocols.smtp_login(smtp, "user@gmail.com", "password")
        self.assertEqual(raised.exception.smtp_code, 534)
        self.assertEqual(smtp.docmd.call_count, 1)
        smtp.login.assert_not_called()


class TestAccountDialogGmail(GoogleFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gmail_gets_app_password_hint_or_google_sign_in(self):
        from mailclient.db import Account
        from mailclient.ui.account_dialog import AccountEditDialog
        dialog = AccountEditDialog(self.db, Account())
        dialog.email.setText("new@gmail.com")
        dialog._apply_preset()
        self.assertEqual(dialog.auth_method.currentData(), "password")
        self.assertFalse(dialog.gmail_hint.isHidden())
        dialog.close()
        self.db.set_setting("google_client_id", self.acc.oauth_client_id)
        self.db.set_setting("google_client_secret", self.acc.oauth_client_secret)
        dialog = AccountEditDialog(self.db, Account())
        dialog.email.setText("new@gmail.com")
        dialog._apply_preset()
        self.assertEqual(dialog.auth_method.currentData(), "google")
        self.assertTrue(dialog.gmail_hint.isHidden())
        collected = dialog._collect()
        self.assertEqual((collected.oauth_client_id, collected.oauth_client_secret),
                         (self.acc.oauth_client_id, self.acc.oauth_client_secret))
        self.assertEqual(collected.imap_host, "imap.gmail.com")
        oauth.validate_config(collected)
        # Переключение на Microsoft не подставляет Client ID Google.
        dialog.auth_method.setCurrentIndex(dialog.auth_method.findData("microsoft"))
        self.assertNotEqual(dialog.oauth_client_id.text(), self.acc.oauth_client_id)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
