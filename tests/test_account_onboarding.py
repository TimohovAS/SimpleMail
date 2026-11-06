"""Onboarding saves a mailbox only after sign-in and both connection checks."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from mailclient import i18n, oauth
from mailclient.db import Account, Database
from mailclient.errors import ErrorInfo
from mailclient.protocols import CheckResult
from mailclient.ui import account_dialog
from mailclient.ui.storage_picker import STORAGE_PARENT_SETTING


class TestAccountOnboarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        i18n.set_language("ru")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "mail.db")
        self.db.set_setting("google_client_id", "123-test.apps.googleusercontent.com")
        self.db.set_setting("google_client_secret", "test-client-secret")
        self.windows = []
        self.warning = self.patch("mailclient.ui.account_dialog.QMessageBox.warning")
        self.patch("mailclient.ui.account_dialog.QMessageBox.information")
        self.patch("mailclient.ui.account_dialog.QMessageBox.question", return_value=QMessageBox.No)
        self.check_results = self.patch("mailclient.ui.account_dialog.show_check_results")
        self.show_error = self.patch("mailclient.ui.account_dialog.show_error")
        self.patch("mailclient.ui.account_dialog.get_password", return_value="stored-password")
        self.set_password = self.patch("mailclient.ui.account_dialog.set_password", return_value=True)
        self.patch("mailclient.ui.account_dialog.delete_password")
        self.patch("mailclient.ui.account_dialog.default_client",
                   return_value={"client_id": "", "client_secret": ""})
        self.patch("mailclient.oauth.default_client",
                   return_value={"client_id": "", "client_secret": ""})
        self.session = self.patch("mailclient.oauth.has_session", return_value=False)
        self.token = self.patch("mailclient.oauth.access_token", return_value="test-access-token")
        self.connection = self.patch("mailclient.ui.account_dialog.test_connection",
                                     return_value=self.success())
        self.task = self.patch("mailclient.ui.account_dialog.run_task")
        self.sign_in = self.patch("mailclient.ui.oauth_dialog.OAuthDialog")
        self.sign_in.return_value.exec.return_value = QDialog.Accepted
        self.sign_in.return_value.persistent = True

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.app.processEvents()
        self.db.close()
        self.temp.cleanup()
        i18n.set_language("ru")

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def success():
        return [CheckResult("IMAP", True), CheckResult("SMTP", True)]

    @staticmethod
    def run_inline(fn, *args, on_done=None, on_error=None, **kwargs):
        result = fn(*args)
        if on_done:
            on_done(result)

    def dialog(self, email="new@gmail.com", account=None):
        window = account_dialog.AccountEditDialog(self.db, account)
        self.windows.append(window)
        if account is None:
            window.email.setText(email)
            window.storage_picker.base_edit.setText(str(self.root / "Mail"))
        return window

    def saved_password_account(self):
        account = Account(name="Existing", email="saved@example.com", username="saved@example.com",
                          imap_host="imap.example.com", smtp_host="smtp.example.com",
                          storage_dir=str(self.root / "Existing"), auth_method="password")
        self.db.save_account(account)
        return account

    def test_email_correction_updates_automatic_identity_servers_and_folder(self):
        dialog = self.dialog("typo@gmail.com")
        dialog.email.setText("corrected@gmail.com")
        self.assertEqual(dialog.username.text(), "corrected@gmail.com")
        self.assertEqual(dialog.name.text(), "corrected@gmail.com")
        self.assertEqual(Path(dialog.storage.text()).name, "corrected@gmail.com")
        dialog.email.setText("corrected@outlook.com")
        self.assertEqual(dialog.username.text(), "corrected@outlook.com")
        self.assertEqual(dialog.imap_host.text(), "outlook.office365.com")
        self.assertEqual(dialog.smtp_host.text(), "smtp.office365.com")
        self.assertEqual(dialog.smtp_port.value(), 587)
        self.assertEqual(dialog.auth_method.currentData(), "microsoft")
        dialog.email.setText("corrected@custom.example")
        self.assertEqual(dialog.imap_host.text(), "")
        self.assertEqual(dialog.smtp_host.text(), "")
        self.assertEqual(dialog.auth_method.currentData(), "password")

    def test_manual_identity_servers_and_auth_choice_survive_email_edit(self):
        dialog = self.dialog()
        dialog.name.setText("Personal archive")
        dialog.username.setText("custom-login")
        dialog.imap_host.setText("imap.custom.example")
        dialog.imap_port.setValue(1993)
        dialog.smtp_host.setText("smtp.custom.example")
        dialog.smtp_port.setValue(1465)
        dialog._use_app_password()
        dialog.email.setText("corrected@yandex.ru")
        self.assertEqual(dialog.name.text(), "Personal archive")
        self.assertEqual(dialog.username.text(), "custom-login")
        self.assertEqual(dialog.imap_host.text(), "imap.custom.example")
        self.assertEqual(dialog.imap_port.value(), 1993)
        self.assertEqual(dialog.smtp_host.text(), "smtp.custom.example")
        self.assertEqual(dialog.smtp_port.value(), 1465)
        self.assertEqual(dialog.auth_method.currentData(), "password")

    def test_saved_client_configuration_is_hidden_and_primary_action_adds_google(self):
        dialog = self.dialog()
        self.assertEqual(dialog.auth_method.currentData(), "google")
        self.assertTrue(dialog.oauth_client_id.isHidden())
        self.assertTrue(dialog.oauth_client_secret.isHidden())
        self.assertTrue(dialog.oauth_login.isHidden())
        self.assertTrue(dialog.advanced.isHidden())
        self.assertEqual(dialog.save_btn.text(), "Войти в Google и добавить")
        self.assertEqual(dialog._collect().oauth_client_id, "123-test.apps.googleusercontent.com")

    def test_one_save_performs_sign_in_checks_both_protocols_then_persists(self):
        dialog = self.dialog()
        events = []
        self.sign_in.return_value.exec.side_effect = lambda: events.append("sign_in") or QDialog.Accepted
        self.token.side_effect = lambda *args: events.append("token") or "test-access-token"
        self.connection.side_effect = lambda *args: events.append("IMAP+SMTP") or self.success()
        self.task.side_effect = self.run_inline
        original_save = self.db.save_account
        with mock.patch.object(self.db, "save_account", side_effect=lambda account:
                               events.append("save") or original_save(account)):
            dialog._save()
        self.assertEqual(events, ["sign_in", "token", "IMAP+SMTP", "save"])
        self.assertEqual(dialog.result(), QDialog.Accepted)
        accounts = self.db.accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "new@gmail.com")
        self.assertEqual(accounts[0].auth_method, "google")
        self.assertEqual(accounts[0].storage_dir, str(self.root / "Mail" / "new@gmail.com"))
        self.assertEqual(self.db.get_setting(STORAGE_PARENT_SETTING), str(self.root / "Mail"))
        self.assertEqual(self.connection.call_args.args[1], "test-access-token")
        self.set_password.assert_not_called()
        self.warning.assert_not_called()

    def test_existing_session_skips_browser_but_still_checks_new_account(self):
        self.session.return_value = True
        self.task.side_effect = self.run_inline
        dialog = self.dialog()
        dialog._save()
        self.sign_in.assert_not_called()
        self.connection.assert_called_once()
        self.assertEqual(len(self.db.accounts()), 1)

    def test_gmail_grouped_app_password_is_normalized_for_check_and_save(self):
        self.task.side_effect = self.run_inline
        dialog = self.dialog()
        dialog._use_app_password()
        dialog.password.setText("abcd efgh ijkl mnop")
        dialog._test()
        self.assertEqual(self.connection.call_args.args[1], "abcdefghijklmnop")
        self.assertEqual(self.db.accounts(), [])
        dialog._save()
        self.assertEqual(self.connection.call_args.args[1], "abcdefghijklmnop")
        self.set_password.assert_called_once_with(self.db, dialog.account.id, "abcdefghijklmnop")
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.sign_in.assert_not_called()

    def test_non_google_password_spacing_is_preserved_for_check_and_save(self):
        self.task.side_effect = self.run_inline
        dialog = self.dialog("new@yandex.ru")
        password = "abcd efgh ijkl mnop"
        dialog.password.setText(password)
        dialog._test()
        self.assertEqual(self.connection.call_args.args[1], password)
        dialog._save()
        self.assertEqual(self.connection.call_args.args[1], password)
        self.set_password.assert_called_once_with(self.db, dialog.account.id, password)
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_cancelling_google_sign_in_does_not_add_or_remember_parent(self):
        self.sign_in.return_value.exec.return_value = QDialog.Rejected
        dialog = self.dialog()
        dialog._save()
        self.assertEqual(self.db.accounts(), [])
        self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))
        self.task.assert_not_called()
        self.assertIn("Вход отменён", dialog.status.text())

    def test_failure_of_either_protocol_does_not_save_and_keeps_input(self):
        self.task.side_effect = self.run_inline
        for failed in ("IMAP", "SMTP"):
            with self.subTest(failed=failed):
                self.connection.side_effect = None
                self.connection.return_value = [
                    CheckResult(name, name != failed,
                                ErrorInfo("Cannot connect", "Retry", "test failure") if name == failed else None)
                    for name in ("IMAP", "SMTP")]
                dialog = self.dialog()
                dialog._save()
                self.assertEqual(self.db.accounts(), [])
                self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))
                self.assertEqual(dialog.email.text(), "new@gmail.com")
                self.assertTrue(dialog.save_btn.isEnabled())
                self.assertFalse(dialog._closed)
                self.check_results.assert_called()

    def test_rejected_dialog_ignores_late_success_and_failure_callbacks(self):
        dialog = self.dialog()
        dialog._save()
        callbacks = self.task.call_args.kwargs
        dialog.reject()
        callbacks["on_done"](self.success())
        callbacks["on_error"](ErrorInfo("Late error", "", "test"))
        self.assertEqual(self.db.accounts(), [])
        self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))
        self.show_error.assert_not_called()

    def test_storage_only_edit_of_existing_password_account_saves_offline(self):
        account = self.saved_password_account()
        dialog = self.dialog(account=account)
        target = str(self.root / "Moved" / account.email)
        dialog.storage.setText(target)
        dialog._save()
        self.task.assert_not_called()
        self.sign_in.assert_not_called()
        self.assertEqual(self.db.account(account.id).storage_dir, target)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.set_password.assert_called_once_with(self.db, account.id, "stored-password")

    def test_duplicate_email_is_rejected_before_sign_in(self):
        existing = self.saved_password_account()
        dialog = self.dialog(existing.email.upper())
        dialog.imap_host.setText(existing.imap_host)
        dialog.smtp_host.setText(existing.smtp_host)
        dialog.password.setText("password")
        dialog._save()
        self.warning.assert_called_once()
        self.assertIn("Этот адрес уже добавлен", self.warning.call_args.args[2])
        self.assertEqual(len(self.db.accounts()), 1)
        self.task.assert_not_called()
        self.sign_in.assert_not_called()
        self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))

    def test_duplicate_storage_is_rejected_before_sign_in(self):
        existing = self.saved_password_account()
        dialog = self.dialog()
        dialog.storage.setText(existing.storage_dir)
        dialog._save()
        self.warning.assert_called_once()
        self.assertIn("Эта папка уже используется", self.warning.call_args.args[2])
        self.assertEqual(len(self.db.accounts()), 1)
        self.task.assert_not_called()
        self.sign_in.assert_not_called()

    def test_relative_storage_path_is_rejected_before_sign_in(self):
        dialog = self.dialog()
        dialog.storage.setText("relative/mail")
        dialog._save()
        self.warning.assert_called_once()
        self.assertIn("Укажите полный путь", self.warning.call_args.args[2])
        self.assertEqual(self.db.accounts(), [])
        self.task.assert_not_called()
        self.sign_in.assert_not_called()

    def test_unwritable_storage_is_rejected_before_sign_in(self):
        dialog = self.dialog()
        with mock.patch("tempfile.TemporaryFile", side_effect=PermissionError("Read-only directory")):
            dialog._save()
        self.warning.assert_called_once()
        self.assertIn("Не удалось создать папку хранения", self.warning.call_args.args[2])
        self.assertEqual(self.db.accounts(), [])
        self.assertFalse(self.db.get_setting(STORAGE_PARENT_SETTING))
        self.task.assert_not_called()
        self.sign_in.assert_not_called()


if __name__ == "__main__":
    unittest.main()
