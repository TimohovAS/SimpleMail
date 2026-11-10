"""Число непрочитанных на кнопке SimpleMail в панели задач Windows."""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

from mailclient.ui import taskbar
from mailclient.ui.tray import BADGE_COLOR


class QtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestOverlayImage(QtCase):
    def colors(self, image):
        return {image.pixelColor(x, y).name() for x in range(image.width()) for y in range(image.height())
                if image.pixelColor(x, y).alpha() > 200}

    def test_yellow_envelope_for_unread_mail(self):
        for size in (16, 24, 32):
            image = taskbar.overlay_image(3, size)
            self.assertEqual((image.width(), image.height()), (size, size))
            colors = self.colors(image)
            self.assertIn(QColor(BADGE_COLOR).name(), colors)
            self.assertNotIn("#dc2626", colors)  # красный — только для ошибок

    def test_nothing_drawn_without_unread_mail(self):
        image = taskbar.overlay_image(0, 16)
        self.assertEqual(self.colors(image), set())
        self.assertEqual({image.pixelColor(x, y).alpha() for x in range(16) for y in range(16)}, {0})

    def test_large_count_still_draws(self):
        self.assertIn(QColor(BADGE_COLOR).name(), self.colors(taskbar.overlay_image(250, 24)))


class TestTaskbarBadge(QtCase):
    def setUp(self):
        self.window = QWidget()
        self.addCleanup(self.window.close)

    def test_does_nothing_outside_windows(self):
        with mock.patch.object(taskbar.sys, "platform", "linux"):
            badge = taskbar.TaskbarBadge(self.window)
        self.assertFalse(badge.available)
        self.window.show()
        badge.set_unread(5)  # без исключений
        self.assertEqual(badge.count, 5)

    def test_hidden_window_is_not_created_early(self):
        """winId() у ещё не показанного окна создал бы его раньше времени (мигание при запуске)."""
        badge = taskbar.TaskbarBadge.__new__(taskbar.TaskbarBadge)
        badge.window, badge.count, badge._applied, badge._taskbar, badge._warned = self.window, 0, None, None, False
        badge._applied_at = 0.0
        badge.available = True
        badge.set_unread(4)
        self.assertIsNone(self.window.windowHandle())

    @unittest.skipUnless(sys.platform == "win32", "только Windows")
    def test_real_overlay_on_windows(self):
        badge = taskbar.TaskbarBadge(self.window)
        self.addCleanup(badge.close)
        self.window.show()
        QApplication.processEvents()
        badge.set_unread(7)
        badge.set_unread(0)
        badge.refresh()
        icon = taskbar.hicon_from_image(taskbar.overlay_image(12, 24))
        self.assertTrue(icon)
        taskbar._user32.DestroyIcon(icon)


if __name__ == "__main__":
    unittest.main()
