"""Переключение на другое письмо без мигания: шапка нового письма сразу, прежний текст сразу
закрыт пустой областью, письмо (или переписка) отрисовывается один раз."""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mailclient.ui import main_window

from test_keep_view import KeepViewCase


class DeferredTasks:
    """Фоновые задачи окна не выполняются сразу: видно, что показано до их окончания."""
    def __init__(self):
        self.queue = []

    def __call__(self, fn, *args, on_done=None, on_error=None, local=False, **kw):
        if local:
            self.queue.append((fn, args, kw, on_done))

    def run_all(self):
        while self.queue:
            fn, args, kw, on_done = self.queue.pop(0)
            result = fn(*args, **kw)
            if on_done:
                on_done(result)


class TestSwitchWithoutFlicker(KeepViewCase):
    def test_header_is_new_and_old_text_hidden_before_message_loads(self):
        first = self.add("Первое", 100, body="Текст первого")
        second = self.add("Второе", 200, body="Текст второго")
        window = self.open_window()
        self.assertTrue(window._select_row(first))
        self.assertFalse(window.viewer.covered)
        tasks = DeferredTasks()
        with mock.patch.object(main_window, "run_task", side_effect=tasks):
            self.assertTrue(window._select_row(second))
            # письмо ещё читается в фоне
            self.assertTrue(window.header.isVisible())  # шапка не прячется — текст не подскакивает
            self.assertEqual(window.header.subject.text(), "Второе")
            self.assertTrue(window.viewer.covered)       # текста первого письма не видно
            tasks.run_all()
        self.assertFalse(window.viewer.covered)
        self.assertEqual(window.viewer.text.toPlainText(), "Текст второго")

    def test_thread_is_drawn_once(self):
        other = self.add("Другое", 50, body="Другое")
        a = self.add("Тема", 100, body="Первое", mid="a@x")
        b = self.add("Re: Тема", 200, body="Второе", mid="b@x", in_reply_to="a@x")
        window = self.open_window(thread_view=True)
        self.assertTrue(window._select_row(other))
        viewer = window.viewer
        with mock.patch.object(viewer, "show_message", side_effect=viewer.show_message) as single, \
                mock.patch.object(viewer, "show_thread", side_effect=viewer.show_thread) as thread:
            self.assertTrue(window._select_row(b))
        single.assert_not_called()  # без промежуточного показа одного письма
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(viewer.thread_ids(), [a, b])
        self.assertEqual(window.header.thread_btn.text(), main_window.tr("Переписка ({0})", 2))
        self.assertFalse(viewer.covered)

    def test_message_of_shown_thread_is_selected_without_cover(self):
        a = self.add("Тема", 100, body="Первое", mid="a@x")
        b = self.add("Re: Тема", 200, body="Второе", mid="b@x", in_reply_to="a@x")
        window = self.open_window(thread_view=True)
        self.assertTrue(window._select_row(b))
        with mock.patch.object(window.viewer, "hold") as hold:
            self.assertTrue(window._select_row(a))
        hold.assert_not_called()  # страница переписки остаётся, выделение переходит на письмо
        self.assertEqual(window._shown.id, a)

    def test_cleared_view_stays_blank(self):
        mid = self.add("Письмо", 100)
        window = self.open_window()
        self.assertTrue(window._select_row(mid))
        window.table.clearSelection()
        self.app.processEvents()
        self.assertTrue(window.viewer.covered)
        self.assertFalse(window.header.isVisible())


if __name__ == "__main__":
    unittest.main()
