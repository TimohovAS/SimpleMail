"""OAuth lifecycle: local callbacks, cancellation and recoverable sign-in failures."""
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from PySide6.QtWidgets import QApplication

from mailclient import errors, google_oauth, oauth
from mailclient.ui.oauth_dialog import OAuthDialog
from test_google_oauth import GoogleFixture, id_token


class TestGoogleCallback(GoogleFixture):
    def complete(self, flow, params, tokens=None):
        browser_errors = []

        def browser():
            try:
                url = flow["redirect"] + "/?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(url, timeout=5) as response:
                    response.read()
            except Exception as error:
                browser_errors.append(error)

        thread = threading.Thread(target=browser, daemon=True)
        thread.start()
        try:
            with mock.patch.object(google_oauth, "_post", return_value=tokens):
                return oauth.finish_sign_in(self.db, self.acc, flow, threading.Event())
        finally:
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertFalse(browser_errors)

    def test_foreign_callback_does_not_interrupt_correct_sign_in(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        browser_errors = []
        pages = []

        def browser():
            try:
                url = flow["redirect"] + "/?code=stale&state=foreign"
                try:
                    urllib.request.urlopen(url, timeout=5)
                except urllib.error.HTTPError as error:
                    pages.append(error.code)
                    error.close()
                params = urllib.parse.urlencode({"code": "valid", "state": flow["state"]})
                with urllib.request.urlopen(flow["redirect"] + "/?" + params, timeout=5) as response:
                    pages.append(response.read().decode())
            except Exception as error:
                browser_errors.append(error)

        thread = threading.Thread(target=browser, daemon=True)
        thread.start()
        tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": id_token(self.acc.email)}
        with mock.patch.object(google_oauth, "_post", return_value=tokens) as post:
            oauth.finish_sign_in(self.db, self.acc, flow, threading.Event())
        thread.join(6)
        self.assertFalse(browser_errors)
        self.assertEqual(pages[0], 400)
        self.assertNotIn("Вход выполнен", pages[1])
        self.assertEqual(post.call_args.args[1]["code"], "valid")
        self.assertTrue(oauth.has_session(self.db, self.acc))

    def test_timeout_closes_server_and_preserves_explanation(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        flow["expires_at"] = 0
        with self.assertRaises(google_oauth.GoogleAuthError) as raised:
            oauth.finish_sign_in(self.db, self.acc, flow, threading.Event())
        self.assertEqual(flow["server"].socket.fileno(), -1)
        self.assertIn("Время ожидания", errors.explain(raised.exception).summary)

    def test_denied_access_explains_how_to_retry(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        with self.assertRaises(google_oauth.GoogleAuthError) as raised:
            self.complete(flow, {"error": "access_denied", "state": flow["state"]})
        self.assertIn("отметьте разрешение", errors.explain(raised.exception).summary)
        self.assertFalse(oauth.has_session(self.db, self.acc))

    def test_partial_consent_without_refresh_token_explains_mail_checkbox(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        tokens = {"access_token": "identity-only", "id_token": id_token(self.acc.email),
                  "scope": "openid https://www.googleapis.com/auth/userinfo.email"}
        with self.assertRaises(google_oauth.GoogleAuthError) as raised:
            self.complete(flow, {"code": "valid", "state": flow["state"]}, tokens)
        self.assertIn("отметьте разрешение", str(raised.exception))
        self.assertIn("Продолжить", str(raised.exception))
        self.assertFalse(oauth.has_session(self.db, self.acc))

    def test_missing_identity_or_gmail_permission_cannot_save_session(self):
        for extra in ({}, {"id_token": id_token(self.acc.email), "scope": "openid email"}):
            with self.subTest(extra=extra):
                flow = oauth.begin_sign_in(self.db, self.acc)
                tokens = {"access_token": "access", "refresh_token": "refresh", **extra}
                with self.assertRaises(google_oauth.GoogleAuthError):
                    self.complete(flow, {"code": "valid", "state": flow["state"]}, tokens)
                self.assertFalse(oauth.has_session(self.db, self.acc))

    def test_google_identity_must_match_mailbox_login(self):
        self.acc.username = "other@gmail.com"
        flow = oauth.begin_sign_in(self.db, self.acc)
        tokens = {"access_token": "access", "refresh_token": "refresh", "id_token": id_token(self.acc.email)}
        with self.assertRaises(google_oauth.GoogleAuthError):
            self.complete(flow, {"code": "valid", "state": flow["state"]}, tokens)

    def test_cancel_after_callback_never_exchanges_code(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        flow["server"].result = {"code": "valid", "state": flow["state"]}
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(google_oauth, "_post") as post:
            self.assertIsNone(oauth.finish_sign_in(self.db, self.acc, flow, cancel))
        post.assert_not_called()
        self.assertEqual(flow["server"].socket.fileno(), -1)

    def test_session_presence_is_local_and_disappears_on_logout(self):
        self.assertFalse(oauth.has_session(self.db, self.acc))
        self.sign_in()
        with mock.patch.object(google_oauth, "_post", side_effect=AssertionError("unexpected network")):
            self.assertTrue(oauth.has_session(self.db, self.acc))
            google_oauth.logout(self.db, self.acc, revoke=False)
            self.assertFalse(oauth.has_session(self.db, self.acc))

    def test_status_does_not_wait_for_refresh_network_lock(self):
        self.sign_in()
        with mock.patch.object(google_oauth, "_lock", side_effect=AssertionError("UI waited for refresh")):
            self.assertTrue(oauth.has_session(self.db, self.acc))
        from dataclasses import replace
        microsoft = replace(self.acc, auth_method="microsoft")
        app = mock.Mock()
        app.get_accounts.return_value = [{"username": microsoft.username}]
        guard = mock.Mock()
        guard.__enter__ = mock.Mock(side_effect=AssertionError("UI waited for Microsoft refresh"))
        context = (app, guard, True)
        with mock.patch.dict(oauth._contexts, {oauth._key(self.db, microsoft): context}):
            self.assertTrue(oauth.has_session(self.db, microsoft))

    def test_configuration_failure_names_action_without_secret_response(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        tokens = {"error": "invalid_client", "error_description": "secret-marker"}
        with self.assertRaises(google_oauth.GoogleAuthError) as raised:
            self.complete(flow, {"code": "valid", "state": flow["state"]}, tokens)
        explanation = errors.explain(raised.exception)
        self.assertIn("Client ID", explanation.summary)
        self.assertNotIn("secret-marker", explanation.details)


class TestOAuthDialogLifecycle(GoogleFixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.jobs = []

        def queue(fn, *args, **kwargs):
            self.jobs.append((fn, args, kwargs))

        patcher = mock.patch("mailclient.ui.oauth_dialog.run_task", side_effect=queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialog = OAuthDialog(self.db, self.acc)
        self.addCleanup(self.dialog.reject)

    def ready(self):
        flow = oauth.begin_sign_in(self.db, self.acc)
        self.addCleanup(flow["server"].server_close)
        self.jobs[0][2]["on_done"](flow)
        return flow

    def test_cancel_during_preparation_closes_late_server(self):
        self.dialog.reject()
        with mock.patch("mailclient.ui.oauth_dialog.QDesktopServices.openUrl") as browser:
            flow = self.ready()
        self.assertEqual(flow["server"].socket.fileno(), -1)
        browser.assert_not_called()
        self.assertEqual(len(self.jobs), 1)

    def test_browser_failure_offers_copy_and_keeps_waiting(self):
        with mock.patch("mailclient.ui.oauth_dialog.QDesktopServices.openUrl", return_value=False):
            self.ready()
        self.assertIn("Скопируйте", self.dialog.status.text())
        self.assertTrue(self.dialog.copy_button.isEnabled())
        self.assertTrue(self.dialog.open_button.isEnabled())
        self.assertEqual(len(self.jobs), 2)

    def test_failed_sign_in_can_retry_and_retains_hint(self):
        with mock.patch("mailclient.ui.oauth_dialog.QDesktopServices.openUrl", return_value=True):
            self.ready()
        self.jobs[1][2]["on_error"](errors.ErrorInfo("Problem", "Do this", "technical"))
        self.assertIn("Do this", self.dialog.status.text())
        self.assertFalse(self.dialog.retry_button.isHidden())
        self.assertFalse(self.dialog.open_button.isEnabled())
        self.dialog.retry_button.click()
        self.assertEqual(len(self.jobs), 3)
        self.assertTrue(self.dialog.retry_button.isHidden())

    def test_empty_completion_does_not_leave_stale_wait_screen(self):
        with mock.patch("mailclient.ui.oauth_dialog.QDesktopServices.openUrl", return_value=True):
            self.ready()
        self.jobs[1][2]["on_done"](None)
        self.assertFalse(self.dialog.retry_button.isHidden())
        self.assertIn("не завершён", self.dialog.status.text())


if __name__ == "__main__":
    unittest.main()
