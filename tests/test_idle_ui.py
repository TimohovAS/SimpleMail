"""IDLE не блокирует Qt и не теряет события во время синхронизации."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mailclient.db import Account, Database
from mailclient.ui import main_window, viewer


class TestIdleUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.ids = [self.db.save_account(Account(name=f"Test {i}", email=f"test{i}@example.com",
                                                storage_dir=self.tmp.name)) for i in range(2)]
        self.db.save_account(Account(name="Disabled", enabled=False, storage_dir=self.tmp.name))
        self.worker_patch = mock.patch.object(main_window, "run_task")
        self.tasks = self.worker_patch.start()
        self.watcher_patch = mock.patch("mailclient.ui.mail_watch.MailWatcher")
        self.watchers = self.watcher_patch.start()
        self.viewer_patch = mock.patch.object(viewer, "HAS_WEBENGINE", False)
        self.viewer_patch.start()
        self.window = main_window.MainWindow(self.db)
        self.app.processEvents()  # немедленная проверка при запуске

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.viewer_patch.stop()
        self.watcher_patch.stop()
        self.worker_patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def finish_sync(self):
        while self.window.syncing:
            self.window._sync_done(0)

    def test_startup_checks_enabled_accounts_and_starts_watchers(self):
        self.assertEqual(self.window.syncing, 2)
        self.assertEqual(self.watchers.call_count, 2)
        synced_ids = [call.args[2].id for call in self.tasks.call_args_list
                      if call.args[0] is main_window.sync_account]
        self.assertEqual(synced_ids, self.ids)

    def test_notification_from_thread_is_queued_and_checks_only_that_account(self):
        self.finish_sync()
        self.tasks.reset_mock()
        thread = threading.Thread(target=self.window.mail_watch.changed.emit, args=(self.ids[1],))
        thread.start()
        thread.join()
        self.assertEqual(self.window._pending_mail, set())
        self.app.processEvents()
        self.assertEqual(self.window._pending_mail, {self.ids[1]})
        self.window._check_pending_mail()
        self.assertEqual(self.window.syncing, 1)
        self.assertEqual(self.tasks.call_args.args[2].id, self.ids[1])

    def test_notification_during_sync_triggers_follow_up(self):
        self.window._mail_changed(self.ids[0])
        self.window._check_pending_mail()
        self.assertEqual(self.window.syncing, 2)
        self.finish_sync()
        self.assertTrue(self.window._mail_timer.isActive())
        self.window._check_pending_mail()
        self.assertEqual(self.window.syncing, 1)
        self.assertEqual(self.tasks.call_args.args[2].id, self.ids[0])
        self.assertEqual(self.window._pending_mail, set())

    def test_busy_manual_check_preserves_error_reporting_mode(self):
        self.finish_sync()
        self.window.check_mail_manual()
        self.window.check_mail()
        self.assertTrue(self.window._manual_check)

    def test_closing_stops_watchers_and_ignores_late_notifications(self):
        self.window.close()
        self.window._mail_changed(self.ids[0])
        self.assertFalse(self.window.timer.isActive())
        self.assertFalse(self.window._mail_timer.isActive())
        self.assertFalse(self.window._ui_save_timer.isActive())
        self.assertEqual(self.window._pending_mail, set())
        self.watchers.return_value.stop.assert_called()

    def test_restart_reads_changed_accounts_and_discards_old_callbacks(self):
        old_callback = self.watchers.call_args_list[0].args[2]
        acc = self.db.account(self.ids[0])
        acc.enabled = False
        self.db.save_account(acc)
        self.watchers.reset_mock()
        self.window.mail_watch.restart()
        self.assertEqual(self.watchers.call_count, 1)
        self.assertEqual(self.watchers.call_args.args[1].id, self.ids[1])
        old_callback(self.ids[0])
        self.app.processEvents()
        self.assertEqual(self.window._pending_mail, set())

    def test_unavailable_idle_with_disabled_timer_is_reported(self):
        self.window.timer.stop()
        self.window._idle_unavailable(self.ids[0])
        self.assertIn("по таймеру отключена", self.window.status.currentMessage())


if __name__ == "__main__":
    unittest.main()
