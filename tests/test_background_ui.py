"""Headers appear during sync; opening one schedules its body without UI races."""
import os
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient import i18n, storage
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

    def refresh_body_progress(self, mid):
        message = self.db.message(mid, with_raw=False)
        self.window._sync_progress(ProgressText("Body arrived", messages_changed=True,
                                               account_id=message.account_id,
                                               message_ids=(mid,), change_kind="body"))
        self.window._sync_refresh_timer.stop()
        self.window._refresh_sync_messages()

    def table_items(self):
        return {self.window.table.item(i, 0).data(main_window.ROLE_ID):
                self.window.table.item(i, main_window.COL["subject"])
                for i in range(self.window.table.rowCount())}

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

    def test_body_progress_updates_only_changed_row_without_reloading_selected_raw(self):
        selected = self.add(complete=True)
        changed, untouched = self.add(), self.add()
        self.select(selected)
        self.window.reload_messages()  # include the read flag set when opening it
        before = self.table_items()
        original_raw = self.window._shown.raw
        self.hydrate(changed)
        with mock.patch.object(storage, "read_eml", wraps=storage.read_eml) as read, \
                mock.patch.object(self.window, "reload_tree") as tree, \
                mock.patch.object(self.window, "_load_thread") as thread, \
                mock.patch.object(self.window.viewer, "_render") as render:
            self.refresh_body_progress(changed)
        read.assert_not_called()
        tree.assert_not_called()
        thread.assert_not_called()
        render.assert_not_called()
        after = self.table_items()
        self.assertIs(after[selected], before[selected])
        self.assertIs(after[untouched], before[untouched])
        self.assertIsNot(after[changed], before[changed])
        row = after[changed].row()
        self.assertEqual(self.window.table.item(row, main_window.COL["status"]).text(), "")
        self.assertEqual(self.window._shown.raw, original_raw)

    def test_other_account_body_progress_does_not_query_current_list(self):
        selected = self.add(complete=True)
        first_account = self.account
        other = Account(name="Other", email="other@example.com", enabled=False,
                        storage_dir=str(Path(self.tmp.name) / "other"))
        self.db.save_account(other)
        self.account = other
        changed = self.add()
        self.account = first_account
        self.window.reload_tree()
        self.select(selected)
        self.hydrate(changed)
        with mock.patch.object(self.db, "search_messages", wraps=self.db.search_messages) as search, \
                mock.patch.object(self.window, "_show_message") as show:
            self.refresh_body_progress(changed)
        search.assert_not_called()
        show.assert_not_called()
        self.assertEqual(self.window._selected_id(), selected)

    def test_background_body_event_hydrates_selected_message_without_callback(self):
        mid = self.add()
        self.select(mid)
        self.hydrate(mid)
        self.refresh_body_progress(mid)
        self.assertTrue(self.window._shown.body_downloaded)
        self.assertIn("Downloaded body 1", self.window.viewer.text.toPlainText())
        self.assertTrue(self.db.message(mid).seen)
        self.assertTrue(self.window.acts["reply"].isEnabled())

    def test_header_batch_reuses_existing_items_and_preserves_selection(self):
        selected, old = self.add(complete=True), self.add()
        self.select(selected)
        before = self.table_items()
        new = self.add()
        with mock.patch.object(storage, "read_eml", wraps=storage.read_eml) as read:
            self.window._sync_progress(ProgressText("Headers arrived", messages_changed=True,
                                                   account_id=self.account.id, change_kind="headers"))
            self.window._sync_refresh_timer.stop()
            self.window._refresh_sync_messages()
        after = self.table_items()
        self.assertEqual(set(after), {selected, old, new})
        self.assertIs(after[old], before[old])
        self.assertEqual(self.window._selected_id(), selected)
        read.assert_not_called()

    def test_body_classification_removes_row_without_rebuilding_other_items(self):
        selected, changed, untouched = self.add(complete=True), self.add(), self.add()
        self.select(selected)
        before = self.table_items()
        self.hydrate(changed)
        self.db.update_message(changed, is_spam=1)
        self.refresh_body_progress(changed)
        after = self.table_items()
        self.assertNotIn(changed, after)
        self.assertIs(after[untouched], before[untouched])
        self.assertEqual(self.window._selected_id(), selected)

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

    def test_slow_folder_read_does_not_block_gui_events(self):
        from mailclient.ui.worker import run_task, local_pool
        mid = self.add(complete=True)
        entered, release = threading.Event(), threading.Event()
        heartbeat = []
        original = self.db.search_messages

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            try:
                return original(*args, **kwargs)
            finally:
                self.db.close()  # close this worker's thread-local connection

        with mock.patch.object(main_window, "run_task", side_effect=run_task), \
                mock.patch.object(self.db, "search_messages", side_effect=slow):
            try:
                self.window.reload_messages(keep_selection=False)
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                QTest.qWait(60)
                self.assertTrue(entered.is_set())
                self.assertEqual(heartbeat, [True])
                self.assertEqual(self.window.table.rowCount(), 0)
            finally:
                release.set()
                local_pool().waitForDone(3000)
                self.app.processEvents()
        self.assertEqual(self.window.table.item(0, 0).data(main_window.ROLE_ID), mid)

    def test_slow_old_folder_result_cannot_replace_new_account(self):
        first = self.add(complete=True)
        other = Account(name="Second", email="second@example.com", enabled=False,
                        storage_dir=str(Path(self.tmp.name) / "second"))
        self.db.save_account(other)
        original = self.account
        self.account = other
        second = self.add(complete=True)
        self.account = original
        self.window.reload_tree()
        jobs = []

        def queued(fn, *args, **kwargs):
            if fn == self.db.search_messages:
                jobs.append((kwargs["on_done"], fn(*args)))

        with mock.patch.object(main_window, "run_task", side_effect=queued):
            self.window.reload_messages()
            self.window.tree.setCurrentItem(self.window.tree.topLevelItem(1).child(0))
            self.assertEqual(self.window.table.rowCount(), 0)
            self.assertIsNone(self.window._shown)
            jobs[1][0](jobs[1][1])
            jobs[0][0](jobs[0][1])
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(self.window.table.item(0, 0).data(main_window.ROLE_ID), second)
        self.assertNotEqual(first, second)

    def test_reading_large_message_is_deferred_and_stale_result_is_discarded(self):
        first, second = self.add(complete=True), self.add(complete=True)
        jobs = []
        with mock.patch.object(main_window, "run_task", side_effect=lambda fn, *a, **kw: jobs.append((fn, a, kw))), \
                mock.patch.object(self.db, "message", wraps=self.db.message) as read:
            self.window._show_message(first)
            self.window._show_message(second)
            read.assert_not_called()
            self.assertIsNone(self.window._shown)
            old, current = jobs[:2]
            current[2]["on_done"](current[0](*current[1]))
            old[2]["on_done"](old[0](*old[1]))
        self.assertEqual(self.window._shown.id, second)
        self.assertFalse(self.db.message(first).seen)

    def test_count_refreshes_coalesce_while_query_is_running(self):
        jobs = []
        with mock.patch.object(main_window, "run_task", side_effect=lambda fn, *a, **kw: jobs.append((fn, a, kw))), \
                mock.patch.object(self.db, "unread_counts", wraps=self.db.unread_counts) as count:
            for _ in range(20):
                self.window._refresh_counts()
            count.assert_not_called()
            self.assertEqual(len(jobs), 1)
            jobs[0][2]["on_done"](jobs[0][0]())
            self.assertEqual(len(jobs), 2)
            jobs[1][2]["on_done"](jobs[1][0]())
            self.assertEqual(count.call_count, 2)
            self.assertFalse(self.window._counts_running)

    def test_large_list_yields_to_events_and_folder_change_cancels_remaining_rows(self):
        for _ in range(300):
            self.add()
        rows = self.db.search_messages(account_id=self.account.id, view="inbox")
        heartbeat = []
        self.window._display_rows(rows, None)
        self.assertEqual(self.window.table.rowCount(), 50)
        QTimer.singleShot(0, lambda: heartbeat.append(self.window.table.rowCount()))
        self.app.processEvents()
        self.assertTrue(heartbeat)
        self.assertLess(heartbeat[0], 300)
        # A request for an empty other folder invalidates queued render batches
        # immediately; they must never add old-account rows back afterwards.
        self.window.current = (self.account.id, "sent")
        self.window.reload_messages(keep_selection=False)
        QTest.qWait(50)
        self.assertEqual(self.window.table.rowCount(), 0)

    def test_target_in_first_page_keeps_extra_row_as_pagination_sentinel(self):
        mids = [self.add() for _ in range(501)]
        self.window._select_after_reload = mids[-1]
        self.window.reload_messages(keep_selection=False)
        self.window._select_after_reload = None
        self.window.reload_messages()  # background refresh during partial paint
        QTest.qWait(250)
        self.assertEqual(self.window._search_limit, 500)
        self.assertEqual(self.window.table.rowCount(), 500)
        self.assertNotIn(mids[0], self.table_items())
        self.assertFalse(self.window.search_more.isHidden())

    def test_async_thread_jump_keeps_page_and_click_context_across_row_batches(self):
        source = self.add(complete=True)
        target = self.add(complete=True, reply_to=source)
        self.db.update_message(target, folder="sent", date_ts=1)
        for _ in range(550):
            self.db.update_message(self.add(), folder="sent")
        self.window.acts["thread_view"].setChecked(True)
        self.select(source)
        self.assertTrue(self.window.viewer.thread_shown)
        jobs = []

        def queued(fn, *args, **kwargs):
            if getattr(kwargs.get("on_done"), "__name__", "") == "selected_done":
                jobs.append((fn, args, kwargs))
            else:
                self.run_task(fn, *args, **kwargs)

        with mock.patch.object(main_window, "run_task", side_effect=queued), \
                mock.patch.object(self.window.viewer, "select_in_thread", wraps=self.window.viewer.select_in_thread) as select, \
                mock.patch.object(self.window.viewer, "clear", wraps=self.window.viewer.clear) as clear:
            self.window._activate_thread_message(target)
            self.assertEqual(len(jobs), 1)
            clear.assert_not_called()
            generation = self.window._search_generation
            self.window._queue_sync_refresh(self.account.id, (source,), "body")
            self.window._sync_refresh_timer.stop()
            self.window._refresh_sync_messages()
            self.assertEqual(self.window._search_generation, generation)
            self.assertEqual(len(jobs), 1)
            fn, args, kwargs = jobs[0]
            kwargs["on_done"](fn(*args))
            QTest.qWait(350)
            clear.assert_not_called()
            self.assertEqual(self.window._selected_id(), target)
            self.assertEqual(self.window._shown.id, target)
            self.assertFalse(select.call_args.kwargs["scroll"])
            self.assertEqual(self.window._search_limit, 1000)

    def test_thread_refresh_replaces_stub_body_without_changing_ids_or_selection(self):
        first = self.add(complete=True)
        second = self.add(reply_to=first)
        self.window.acts["thread_view"].setChecked(True)
        self.select(first)
        self.assertEqual(self.window.viewer.thread_state(), {(first, True), (second, False)})
        self.assertIn("Нажмите на письмо", self.window.viewer._page_html)
        self.hydrate(second)
        self.refresh_body_progress(second)
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
