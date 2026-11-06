"""Headers appear during sync; opening one schedules its body without UI races."""
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient import i18n
from mailclient.db import Account, Database
from mailclient.errors import ErrorInfo
from mailclient.mailparse import parse_message
from mailclient.sync import ProgressText
from mailclient.ui import main_window, viewer


class TestBackgroundBodyUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        i18n.set_language("ru")
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.account = Account(name="Test", email="me@example.com", enabled=False,
                               storage_dir=str(Path(self.tmp.name) / "mail"))
        self.db.save_account(self.account)
        self.db.set_setting("auto_updates", "0")
        self.db.set_setting("close_to_tray", "0")
        self.db.set_setting("thread_view", "0")
        self.tasks, self.raws = [], {}
        self.uid = 0
        self.patches = [
            mock.patch.object(main_window, "run_task", side_effect=self.run_task),
            mock.patch.object(main_window.MainWindow, "check_mail"),
            mock.patch.object(main_window.MainWindow, "_status_error"),
            mock.patch("mailclient.ui.mail_watch.MailWatcher"),
            mock.patch.object(viewer, "HAS_WEBENGINE", False),
            mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes),
        ]
        for patch in self.patches:
            patch.start()
        self.window = main_window.MainWindow(self.db)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window._quitting = True
        self.window.close()
        self.app.processEvents()
        for patch in reversed(self.patches):
            patch.stop()
        self.db.close()
        self.tmp.cleanup()
        i18n.set_language("ru")

    def run_task(self, fn, *args, **kwargs):
        if fn is main_window.fetch_message_body:
            self.tasks.append((args, kwargs))
        elif kwargs.get("local"):
            # Local rendering/attachment jobs execute; network work stays mocked.
            result = fn(*args)
            if kwargs.get("on_done"):
                kwargs["on_done"](result)

    def add(self, complete=False, reply_to=None, body=None):
        self.uid += 1
        em = EmailMessage()
        em["From"] = "Sender <sender@example.com>"
        em["To"] = "me@example.com"
        em["Subject"] = "Conversation" if reply_to is None else "Re: Conversation"
        em["Message-ID"] = f"<message-{self.uid}@example.com>"
        if reply_to:
            em["In-Reply-To"] = self.db.message(reply_to).message_id
        em.set_content(body if body is not None else f"Downloaded body {self.uid}")
        raw = em.as_bytes()
        message = parse_message(raw)
        message.account_id = self.account.id
        message.server_folder, message.uidvalidity, message.uid = "INBOX", 42, self.uid
        message.date_ts = 1700000000 + self.uid
        message.body_downloaded = complete
        if not complete:
            message.raw = b""
            message.body_text = message.body_html = ""
        mid = self.db.add_message(message)
        self.raws[mid] = raw
        return mid

    def select(self, mid):
        self.window.reload_messages()
        self.assertTrue(self.window._select_row(mid))
        self.app.processEvents()

    def hydrate(self, mid):
        old = self.db.message(mid)
        full = parse_message(self.raws[mid])
        for field in ("account_id", "server_folder", "uidvalidity", "uid", "folder", "date_ts"):
            setattr(full, field, getattr(old, field))
        self.assertTrue(self.db.complete_message_body(mid, full))
        return self.db.message(mid)

    def test_progress_reveals_headers_before_sync_finishes(self):
        mid = self.add()
        self.window.syncing = 1
        self.assertEqual(self.window.table.rowCount(), 0)
        self.window._sync_progress(ProgressText("Headers arrived", done=1, total=20, messages_changed=True))
        self.assertTrue(self.window._sync_refresh_timer.isActive())
        QTest.qWait(330)
        self.assertEqual(self.window.syncing, 1)
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(self.window.table.item(0, 0).data(main_window.ROLE_ID), mid)
        self.assertEqual(self.window.sync_bar.value(), 1)
        self.assertIsNone(self.window._shown)
        self.assertFalse(self.db.message(mid).seen)

    def test_unselected_download_progress_does_not_rerender_empty_viewer(self):
        mids = [self.add() for _ in range(3)]
        self.window.syncing = 1
        self.window.reload_tree()
        with mock.patch.object(self.window.viewer, "_render", wraps=self.window.viewer._render) as render:
            for count, mid in enumerate(mids, 1):
                self.hydrate(mid)
                self.window._sync_progress(ProgressText("Body arrived", done=count, total=3,
                                                       messages_changed=True))
                QTest.qWait(330)
            render.assert_not_called()
        self.assertIsNone(self.window._shown)
        self.assertEqual(self.window.table.rowCount(), 3)

    def test_unrelated_downloads_preserve_selected_body_and_scroll_without_render(self):
        selected = self.add(complete=True, body="\n".join(f"Reading line {i}" for i in range(150)))
        others = [self.add() for _ in range(40)]
        self.select(selected)
        body_bar = self.window.viewer.text.verticalScrollBar()
        list_bar = self.window.table.verticalScrollBar()
        self.assertGreater(body_bar.maximum(), 0)
        self.assertGreater(list_bar.maximum(), 0)
        body_bar.setValue(body_bar.maximum() // 2)
        list_bar.setValue(list_bar.maximum() // 2)
        body_position = body_bar.value()
        list_view = self.window._list_view()
        with mock.patch.object(self.window.viewer, "_render", wraps=self.window.viewer._render) as render:
            for mid in others[:3]:
                self.hydrate(mid)
                self.window._refresh_sync_messages()
                self.app.processEvents()
            render.assert_not_called()
        self.assertEqual(self.window._shown.id, selected)
        self.assertEqual(self.window._selected_ids(), [selected])
        self.assertEqual(body_bar.value(), body_position)
        self.assertEqual(self.window._list_view(), list_view)

    def test_selecting_header_starts_one_worker_and_disables_body_actions(self):
        mid = self.add()
        self.select(mid)
        self.window.reload_tree()
        self.window.reload_messages()
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(self.tasks[0][0], (self.db, mid))
        self.assertFalse(self.db.message(mid).seen)
        self.assertFalse(self.window.body_notice.isHidden())
        self.assertTrue(self.window.quick.isHidden())
        self.assertTrue(all(not button.isEnabled() for button in self.window.header._body_buttons))
        for key in ("reply", "reply_all", "forward", "redirect", "save_eml", "save_att"):
            self.assertFalse(self.window.acts[key].isEnabled(), key)
        with mock.patch.object(self.window, "_open_compose") as compose, \
                mock.patch.object(self.db, "save_composition") as save:
            self.window._reply()
            self.window._reply_all()
            self.window._forward()
            self.window._quick_reply("No incomplete quoted reply")
        compose.assert_not_called()
        save.assert_not_called()

    def test_body_callback_renders_same_id_and_only_then_marks_read(self):
        mid = self.add()
        self.select(mid)
        callbacks = self.tasks[0][1]
        complete = self.hydrate(mid)
        callbacks["on_done"](complete)
        self.assertEqual(self.window._shown.id, mid)
        self.assertTrue(self.window._shown.body_downloaded)
        self.assertIn("Downloaded body 1", self.window.viewer.text.toPlainText())
        self.assertTrue(self.db.message(mid).seen)
        self.assertTrue(self.window.body_notice.isHidden())
        self.assertTrue(self.window.acts["reply"].isEnabled())
        self.assertEqual(len(self.tasks), 1)
        self.assertTrue(self.window._sync_refresh_timer.isActive())
        QTest.qWait(330)
        self.assertTrue(self.window.table.isColumnHidden(main_window.COL["status"]))

    def test_download_failure_exposes_retry_without_autoretry_on_refresh(self):
        mid = self.add()
        self.select(mid)
        self.tasks[0][1]["on_error"](ErrorInfo("Offline", "Reconnect", "test"))
        self.assertFalse(self.window.body_retry.isHidden())
        self.assertIn("Offline", self.window.body_notice_text.text())
        self.window.reload_tree()
        self.window.reload_messages()
        self.assertEqual(len(self.tasks), 1)
        self.window.body_retry.click()
        self.assertEqual(len(self.tasks), 2)
        self.assertNotIn(mid, self.window._body_errors)
        self.assertTrue(self.window.body_retry.isHidden())

    def test_stale_body_callback_does_not_replace_newly_selected_message(self):
        first, second = self.add(), self.add(complete=True)
        self.select(first)
        first_callbacks = self.tasks[0][1]
        self.select(second)
        self.assertTrue(first_callbacks["cancel"].is_set())
        first_callbacks["on_done"](self.hydrate(first))
        self.assertEqual(self.window._shown.id, second)
        self.assertIn("Downloaded body 2", self.window.viewer.text.toPlainText())
        self.assertFalse(self.db.message(first).seen)

    def test_cancelled_request_reselected_before_error_starts_fresh_request(self):
        first, second = self.add(), self.add(complete=True)
        self.select(first)
        callbacks = self.tasks[0][1]
        self.select(second)
        self.select(first)
        self.assertEqual(len(self.tasks), 1)
        self.assertTrue(callbacks["cancel"].is_set())
        callbacks["on_error"](ErrorInfo("Cancelled socket", "", "test"))
        self.assertEqual(len(self.tasks), 2)
        self.assertFalse(self.tasks[1][1]["cancel"].is_set())
        self.assertNotIn(first, self.window._body_errors)

    def test_cancelled_request_reselected_before_done_starts_fresh_request(self):
        first, second = self.add(), self.add(complete=True)
        self.select(first)
        callbacks = self.tasks[0][1]
        self.select(second)
        self.select(first)
        callbacks["on_done"](None)
        self.assertEqual(len(self.tasks), 2)
        self.assertFalse(self.tasks[1][1]["cancel"].is_set())
        self.assertNotIn(first, self.window._body_errors)

    def test_closed_window_ignores_late_body_callbacks(self):
        mid = self.add()
        self.select(mid)
        callbacks = self.tasks[0][1]
        self.window._quitting = True
        self.window.close()
        self.assertTrue(callbacks["cancel"].is_set())
        with mock.patch.object(self.window, "_show_message") as show:
            callbacks["on_done"](None)
            callbacks["on_error"](ErrorInfo("Late error", "", "test"))
        show.assert_not_called()
        self.assertEqual(len(self.tasks), 1)

    def test_thread_refresh_replaces_stub_body_without_changing_ids_or_selection(self):
        first = self.add(complete=True)
        second = self.add(reply_to=first)
        self.window.acts["thread_view"].setChecked(True)
        self.select(first)
        self.assertEqual(self.window.viewer.thread_state(), {(first, True), (second, False)})
        self.assertIn("Нажмите на письмо", self.window.viewer._page_html)
        self.hydrate(second)
        self.window.reload_messages()
        self.assertEqual(self.window._shown.id, first)
        self.assertEqual(self.window.viewer.thread_state(), {(first, True), (second, True)})
        self.assertIn("Downloaded body 2", self.window.viewer._page_html)
        self.assertNotIn("Нажмите на письмо", self.window.viewer._page_html)
        self.assertFalse(self.db.message(second).seen)

    def test_concurrent_thread_refreshes_render_downloaded_body_once(self):
        first = self.add(complete=True)
        second = self.add(reply_to=first)
        self.window.acts["thread_view"].setChecked(True)
        self.select(first)
        self.hydrate(second)
        callbacks = []

        def queued(fn, *args, **kwargs):
            callbacks.append((kwargs["on_done"], fn(*args)))

        with mock.patch.object(main_window, "run_task", side_effect=queued):
            self.window._load_thread(self.window._shown, refresh=True)
            self.window._load_thread(self.window._shown, refresh=True)
        with mock.patch.object(self.window.viewer, "_render", wraps=self.window.viewer._render) as render:
            for done, result in callbacks:
                done(result)
            self.assertEqual(render.call_count, 1)
        self.assertEqual(self.window.viewer.thread_state(), {(first, True), (second, True)})

    def test_out_of_order_thread_callback_does_not_restore_incomplete_body(self):
        first = self.add(complete=True)
        second, third = self.add(reply_to=first), self.add(reply_to=first)
        self.window.acts["thread_view"].setChecked(True)
        self.select(first)
        callbacks = []

        def queued(fn, *args, **kwargs):
            callbacks.append((kwargs["on_done"], fn(*args)))

        with mock.patch.object(main_window, "run_task", side_effect=queued):
            self.hydrate(second)
            self.window._load_thread(self.window._shown, refresh=True)
            self.hydrate(third)
            self.window._load_thread(self.window._shown, refresh=True)
        with mock.patch.object(self.window.viewer, "_render", wraps=self.window.viewer._render) as render:
            for done, result in reversed(callbacks):
                done(result)
            self.assertEqual(render.call_count, 1)
        self.assertEqual(self.window.viewer.thread_state(), {(first, True), (second, True), (third, True)})
        self.assertNotIn("Нажмите на письмо", self.window.viewer._page_html)


if __name__ == "__main__":
    unittest.main()
