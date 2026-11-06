"""После проверки почты окно не сбрасывается: открытое письмо, его прокрутка, выделение
и прокрутка списка писем и дерева папок остаются как были."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient.db import Account, Database, Message
from mailclient.mailparse import parse_message
from mailclient.ui import main_window, viewer

LONG = "\n".join(f"Строка {i}" for i in range(400))


def raw_mail(mid, subject, body, in_reply_to=""):
    headers = [f"From: Ivan <ivan@el.rs>", "To: me@aviora.rs", f"Subject: {subject}", f"Message-ID: <{mid}>",
               "Date: Mon, 2 Nov 2026 09:00:00 +0100", "MIME-Version: 1.0",
               "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


def run_thread_work(fn, *args, on_done=None, on_error=None, **kw):
    """Фоновые задачи: выполняем только сборку переписки (проверка почты здесь не нужна)."""
    if getattr(fn, "__name__", "") != "work":
        return
    result = fn(*args, **kw)
    if on_done:
        on_done(result)


class KeepViewCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.save_account(Account(name="Ящик", email="me@aviora.rs", storage_dir=self.tmp.name))
        self.uid = 0

    def add(self, subject, ts, body="text", mid=None, in_reply_to=""):
        self.uid += 1
        m = parse_message(raw_mail(mid or f"m{self.uid}@x", subject, body, in_reply_to))
        m.account_id, m.folder, m.server_folder, m.uid, m.date_ts, m.seen = (
            self.acc, "inbox", "INBOX", self.uid, ts, True)
        return self.db.add_message(m)

    def open_window(self, thread_view=False):
        patches = [mock.patch.object(main_window, "run_task", side_effect=run_thread_work),
                   mock.patch("mailclient.ui.mail_watch.MailWatcher"),
                   mock.patch.object(main_window.MainWindow, "check_mail"),
                   mock.patch.object(viewer, "HAS_WEBENGINE", False)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.db.set_setting("thread_view", "1" if thread_view else "0")
        window = main_window.MainWindow(self.db)
        window.resize(1100, 700)
        window.show()
        self.app.processEvents()

        def close():
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                window.close()
            self.app.processEvents()
        self.addCleanup(close)
        return window

    def check_mail_finished(self, window):
        """То, что делает окно по окончании проверки почты: дерево и список перестраиваются."""
        window.reload_tree()
        self.app.processEvents()


class TestOpenMessageStays(KeepViewCase):
    def test_open_message_keeps_scroll_and_is_not_redrawn(self):
        long_id = self.add("Длинное", 100, body=LONG)
        window = self.open_window()
        self.assertTrue(window._select_row(long_id))
        bar = window.viewer.text.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum() // 2)
        position = bar.value()
        window.quick.edit.setPlainText("Набираю ответ")
        self.add("Новое письмо", 200)
        with mock.patch.object(window.viewer, "show_message") as redraw:
            self.check_mail_finished(window)
        redraw.assert_not_called()
        self.assertEqual(window._selected_ids(), [long_id])
        self.assertEqual(window._shown.id, long_id)
        self.assertEqual(bar.value(), position)
        self.assertEqual(window.quick.edit.toPlainText(), "Набираю ответ")

    def test_message_marked_unread_stays_unread_after_check(self):
        mid = self.add("Письмо", 100)
        window = self.open_window()
        self.assertTrue(window._select_row(mid))
        self.db.update_message(mid, seen=0)  # «Отметить непрочитанным»
        self.check_mail_finished(window)
        self.assertFalse(self.db.message(mid, with_raw=False).seen)

    def test_changed_message_is_shown_again(self):
        mid = self.add("Черновик", 100, body="Было")
        window = self.open_window()
        self.assertTrue(window._select_row(mid))
        self.db.update_message(mid, body_text="Стало")  # черновик пересохранён под тем же номером
        window.reload_messages()
        self.assertEqual(window.viewer.text.toPlainText(), "Стало")

    def test_other_message_is_still_shown_normally(self):
        first = self.add("Первое", 100, body="Первое")
        second = self.add("Второе", 200, body="Второе")
        window = self.open_window()
        self.assertTrue(window._select_row(first))
        self.assertEqual(window.viewer.text.toPlainText(), "Первое")
        self.assertTrue(window._select_row(second))
        self.assertEqual(window.viewer.text.toPlainText(), "Второе")


class TestListStays(KeepViewCase):
    def test_several_selected_messages_stay_selected(self):
        ids = [self.add(f"Письмо {i}", 100 + i) for i in range(5)]
        window = self.open_window()
        window.table.clearSelection()
        for mid in (ids[1], ids[3]):
            row = next(r for r in range(window.table.rowCount()) if window.table.item(r, 0).data(main_window.ROLE_ID) == mid)
            window.table.selectionModel().select(window.table.model().index(row, 0),
                                                 main_window.QItemSelectionModel.Select | main_window.QItemSelectionModel.Rows)
        self.assertEqual(sorted(window._selected_ids()), sorted([ids[1], ids[3]]))
        self.add("Новое", 500)
        self.check_mail_finished(window)
        self.assertEqual(sorted(window._selected_ids()), sorted([ids[1], ids[3]]))

    def test_scrolled_list_stays_on_the_same_messages(self):
        ids = [self.add(f"Письмо {i}", 100 + i) for i in range(120)]
        window = self.open_window()
        table = window.table
        self.assertTrue(window._select_row(ids[60]))
        table.verticalScrollBar().setValue(table.verticalScrollBar().maximum() // 2)
        self.app.processEvents()
        top = table.item(table.rowAt(0), 0).data(main_window.ROLE_ID)
        for i in range(3):
            self.add(f"Новое {i}", 1000 + i)
        self.check_mail_finished(window)
        self.assertEqual(table.item(table.rowAt(0), 0).data(main_window.ROLE_ID), top)
        self.assertEqual(window._selected_ids(), [ids[60]])

    def test_list_at_top_shows_new_mail(self):
        for i in range(120):
            self.add(f"Письмо {i}", 100 + i)
        window = self.open_window()
        table = window.table
        table.verticalScrollBar().setValue(0)
        new = self.add("Новое", 1000)
        self.check_mail_finished(window)
        self.assertEqual(table.verticalScrollBar().value(), 0)
        self.assertEqual(table.item(table.rowAt(0), 0).data(main_window.ROLE_ID), new)

    def test_folder_change_does_not_open_new_message(self):
        # Первое письмо само не открывается: иначе новое письмо становилось прочитанным.
        first = self.add("Входящее", 100)
        self.db.update_message(first, seen=0)
        window = self.open_window()
        self.assertEqual(window._selected_ids(), [])
        window.tree.setCurrentItem(window._folder_item(self.acc, "sent"))
        self.assertEqual(window._selected_ids(), [])
        window.tree.setCurrentItem(window._folder_item(self.acc, "inbox"))
        self.assertEqual(window._selected_ids(), [])
        self.assertFalse(self.db.message(first).seen)
        self.assertTrue(window._select_row(first))
        self.assertTrue(self.db.message(first).seen)


class TestThreadStays(KeepViewCase):
    def test_thread_is_rebuilt_only_when_it_changes_and_keeps_scroll(self):
        a = self.add("Тема", 100, body=LONG, mid="a@x")
        b = self.add("Re: Тема", 200, body=LONG, mid="b@x", in_reply_to="a@x")
        window = self.open_window(thread_view=True)
        self.assertTrue(window._select_row(a))
        self.assertEqual(window.viewer.thread_ids(), [a, b])
        bar = window.viewer.text.verticalScrollBar()
        bar.setValue(bar.maximum() // 3)
        position = bar.value()
        show_thread = window.viewer.show_thread
        with mock.patch.object(window.viewer, "show_thread", side_effect=show_thread) as rebuild:
            self.add("Другое", 300)
            self.check_mail_finished(window)
            rebuild.assert_not_called()  # переписка не изменилась
            self.assertEqual(bar.value(), position)
            c = self.add("Re: Тема", 400, body="Третье", mid="c@x", in_reply_to="b@x")
            self.check_mail_finished(window)
            # перестроена один раз и с прежней прокруткой
            self.assertEqual([c.kwargs.get("keep_view") for c in rebuild.call_args_list][:1], [True])
            self.assertEqual(sum(1 for c in rebuild.call_args_list if c.kwargs.get("keep_view")), 1)
        self.assertEqual(window.viewer.thread_ids(), [a, b, c])
        self.assertEqual(window.header.thread_btn.text(), main_window.tr("Переписка ({0})", 3))
        self.assertEqual(window._shown.id, a)
        self.assertEqual(bar.value(), position)


class TestViewerKeepView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_text_view_keeps_scroll(self):
        with mock.patch.object(viewer, "HAS_WEBENGINE", False):
            v = viewer.MessageView()
        v.resize(600, 400)
        v.show()
        m = Message(id=1, subject="S", body_text=LONG)
        v.show_message(m)
        self.app.processEvents()
        bar = v.text.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        position = bar.value()
        v.show_message(m, keep_view=True)
        self.assertEqual(bar.value(), position)
        v.show_message(m)
        self.assertEqual(bar.value(), 0)  # обычный показ — с начала письма

    def test_scripts_read_and_restore_reading_place(self):
        self.assertIn("details.msg", viewer._CAPTURE_VIEW_JS)
        self.assertIn("window.scrollTo", viewer._RESTORE_VIEW_JS)
        self.assertIn('{"y": 10}', viewer._RESTORE_VIEW_JS % '{"y": 10}')


if __name__ == "__main__":
    unittest.main()
