"""An account downloading history must not postpone another account's headers."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient.db import Account, Database
from mailclient.errors import ErrorInfo
from mailclient.ui import main_window, viewer


class TestMultiAccountSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.db.set_setting("auto_updates", "0")
        self.db.set_setting("history_ask", "0")
        self.db.set_setting("close_to_tray", "0")
        self.first = self.add_account("First")
        self.jobs = []
        self.patches = [
            mock.patch.object(main_window, "run_task", side_effect=self.record_task),
            mock.patch("mailclient.ui.mail_watch.MailWatcher"),
            mock.patch.object(viewer, "HAS_WEBENGINE", False),
        ]
        for patch in self.patches:
            patch.start()
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()
        self.assertEqual(self.ids(), [self.first])

    def tearDown(self):
        self.window._quitting = True
        self.window.close()
        self.app.processEvents()
        for patch in reversed(self.patches):
            patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def add_account(self, name, enabled=True):
        return self.db.save_account(Account(name=name, email=f"{name.lower()}@example.invalid",
                                            enabled=enabled, storage_dir=self.tmp.name))

    def record_task(self, fn, *args, **kwargs):
        if fn is main_window.sync_account:
            # Deliberately retain the first task without completing it. Recording
            # another dispatch proves that its header-first backend can run now.
            self.jobs.append((args[1].id, kwargs))

    def ids(self):
        return [account_id for account_id, _ in self.jobs]

    def test_new_accounts_start_while_first_body_task_remains_blocked(self):
        second, third = self.add_account("Second"), self.add_account("Third")
        first_cancel = self.jobs[0][1]["cancel"]
        self.window._mail_changed(second)
        self.window._idle_changed(third)
        self.assertTrue(self.window._mail_timer.isActive())
        self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second, third])
        self.assertEqual(self.window.syncing, 3)
        self.assertFalse(self.jobs[1][1]["inbox_only"])
        self.assertTrue(self.jobs[2][1]["inbox_only"])
        self.assertTrue(all(job[1]["cancel"] is first_cancel for job in self.jobs))
        self.assertEqual(self.window._sync_accounts, {self.first, second, third})

    def test_repeated_notifications_coalesce_and_follow_up_does_not_wait_for_other_account(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        for _ in range(3):
            self.window._idle_changed(second)
            self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second])
        self.assertEqual(self.window._pending_mail, {second})
        # The second account completes; the first still has no completion callback.
        self.jobs[1][1]["on_done"](4)
        self.assertEqual(self.window.syncing, 1)
        self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second, second])
        self.assertEqual(self.window.syncing, 2)
        self.assertEqual(self.window._new_total, 4)
        self.assertEqual(self.window._pending_mail, set())

    def test_busy_notifications_do_not_keep_postponing_another_accounts_dispatch(self):
        second = self.add_account("Second")
        with mock.patch.object(self.window._mail_timer, "start", wraps=self.window._mail_timer.start) as start:
            self.window._mail_changed(second)
            for _ in range(5):
                self.window._idle_changed(self.first)
            start.assert_called_once_with()
        self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second])

    def test_error_releases_only_its_account_and_retains_batch_results(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        self.window._mail_changed(second)
        error = ErrorInfo("Unavailable", "Synthetic test failure", "")
        self.jobs[1][1]["on_error"](error)
        self.assertEqual(self.window._sync_accounts, {self.first})
        self.assertEqual(self.window.syncing, 1)
        self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second, second])
        self.assertEqual(self.window._errors, [("Second", error)])

    def test_stop_cancels_joined_accounts_without_automatically_restarting_pending_work(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        self.window._mail_changed(second)
        self.window._stop_check()
        self.assertTrue(all(job[1]["cancel"].is_set() for job in self.jobs))
        self.window._check_pending_mail()
        self.assertEqual(len(self.jobs), 2)
        self.jobs[0][1]["on_done"](0)
        self.jobs[1][1]["on_done"](0)
        self.assertEqual(self.window.syncing, 0)
        self.assertFalse(self.window._mail_timer.isActive())
        self.window.check_mail(manual=True, account_ids={second})
        self.assertEqual(self.ids(), [self.first, second, second])
        self.assertFalse(self.jobs[-1][1]["cancel"].is_set())

    def test_history_request_starts_idle_account_and_keeps_pause_events_effective(self):
        old_history_cancel = self.jobs[0][1]["history_cancel"]
        self.db.set_setting(f"history:{self.first}", "running")
        self.window._pause_history()
        self.assertTrue(old_history_cancel.is_set())
        second = self.add_account("Second")
        self.window._download_history()
        self.assertEqual(self.ids(), [self.first, second])
        self.assertEqual(self.window._pending_mail, {self.first})
        new_history_cancel = self.jobs[1][1]["history_cancel"]
        self.assertIsNot(new_history_cancel, old_history_cancel)
        self.assertFalse(new_history_cancel.is_set())
        self.window._pause_history()
        self.assertTrue(old_history_cancel.is_set())
        self.assertTrue(new_history_cancel.is_set())
        self.assertEqual(self.db.get_setting(f"history:{second}"), "paused")

    def test_enabled_account_starts_and_disabled_pending_account_is_removed(self):
        second = self.add_account("Second", enabled=False)
        third = self.add_account("Third", enabled=False)
        self.window._set_account_enabled(second, True)
        self.window._mail_changed(third)
        self.window._check_pending_mail()
        self.assertEqual(self.ids(), [self.first, second])
        self.assertNotIn(third, self.window._pending_mail)

    def enable_history_question(self, *account_ids):
        self.db.set_setting("history_ask", "1")
        for account_id in account_ids:
            self.db.set_setting(f"history_skipped:{account_id}:INBOX", "100")

    def test_completed_account_gets_history_question_while_other_account_downloads(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        unchecked = self.add_account("Not checked")
        self.enable_history_question(self.first, second, unchecked)
        shown = []
        with mock.patch.object(QMessageBox, "exec", lambda box: shown.append(box.text())):
            self.jobs[1][1]["on_done"](0)
        self.assertEqual(len(shown), 1)
        self.assertIn("Second", shown[0])
        self.assertEqual(self.window._sync_accounts, {self.first})
        self.assertEqual(self.window._history_not_now, {second})

    def test_completion_during_modal_question_is_offered_after_download_choice(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        self.enable_history_question(self.first, second)
        shown = []

        def nested_completion(box):
            shown.append(box.text())
            if len(shown) == 1:
                self.jobs[1][1]["on_done"](0)
                self.assertEqual(len(shown), 1, "A nested completion opened a second modal question")
            else:
                self.assertIn(self.first, self.window._sync_accounts,
                              "The first history download must not delay the second question")

        with mock.patch.object(QMessageBox, "exec", nested_completion), \
                mock.patch.object(QMessageBox, "clickedButton", lambda box: box.buttons()[0]):
            self.jobs[0][1]["on_done"](0)
        self.assertEqual(len(shown), 2)
        self.assertIn("First", shown[0])
        self.assertIn("Second", shown[1])
        self.assertEqual(self.ids(), [self.first, second, self.first, second])
        self.assertEqual(self.window._sync_accounts, {self.first, second})
        self.assertEqual(self.window._history_offer_accounts, set())

    def test_stop_during_history_question_does_not_start_chosen_download_or_more_questions(self):
        second = self.add_account("Second")
        self.window._mail_changed(second)
        self.window._check_pending_mail()
        self.enable_history_question(self.first, second)
        shown = []

        def stop_during_question(box):
            shown.append(box.text())
            self.window._stop_check()
            self.jobs[1][1]["on_done"](0)

        with mock.patch.object(QMessageBox, "exec", stop_during_question), \
                mock.patch.object(QMessageBox, "clickedButton", lambda box: box.buttons()[0]):
            self.jobs[0][1]["on_done"](0)
        self.assertEqual(len(shown), 1)
        self.assertEqual(self.ids(), [self.first, second])
        self.assertIsNone(self.db.get_setting(f"history:{self.first}"))

    def test_failed_or_closing_account_does_not_offer_history(self):
        self.enable_history_question(self.first)
        with mock.patch.object(QMessageBox, "exec") as question:
            self.jobs[0][1]["on_error"](ErrorInfo("Unavailable", "Synthetic test failure", ""))
            question.assert_not_called()
            self.window._closing = True
            self.window._offer_history({self.first})
            question.assert_not_called()
        self.window._closing = False


if __name__ == "__main__":
    unittest.main()
