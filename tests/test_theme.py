"""Автотема обновляет открытые окна и не меняет отправляемые письма."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPalette, QTextCursor
from PySide6.QtWidgets import QApplication, QMessageBox

from mailclient.db import Account, Database, Message
from mailclient.ui import main_window, viewer
from mailclient.ui.account_dialog import AccountEditDialog
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.icons import icon
from mailclient.ui.theme import SystemTheme


class StyleHints(QObject):
    colorSchemeChanged = Signal(Qt.ColorScheme)

    def __init__(self, scheme):
        super().__init__()
        self.scheme = scheme

    def colorScheme(self):
        return self.scheme

    def change(self, scheme):
        self.scheme = scheme
        self.colorSchemeChanged.emit(scheme)


class TestSystemTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.original_palette = self.app.palette()
        self.original_stylesheet = self.app.styleSheet()
        self.hints = StyleHints(Qt.ColorScheme.Light)
        patch = mock.patch.object(self.app, "styleHints", return_value=self.hints)
        patch.start()
        self.addCleanup(patch.stop)
        self.theme = SystemTheme(self.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "mail.db")
        self.addCleanup(self.db.close)
        self.account = self.db.save_account(Account(email="test@example.com", storage_dir=self.tmp.name))
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                window.close()
        self.app.processEvents()
        self.theme._timer.stop()
        self.hints.colorSchemeChanged.disconnect()
        self.theme.deleteLater()
        self.app.setPalette(self.original_palette)
        self.app.setStyleSheet(self.original_stylesheet)
        self.app.processEvents()

    def switch(self, scheme):
        self.hints.change(scheme)
        for _ in range(3):
            self.app.processEvents()

    def test_existing_widgets_follow_system_changes_in_both_directions(self):
        dialog = AccountEditDialog(self.db)
        self.windows.append(dialog)
        dialog.show()
        self.switch(Qt.ColorScheme.Dark)
        palette = dialog.email.palette()
        self.assertLess(palette.color(QPalette.Base).lightness(), 128)
        self.assertGreater(palette.color(QPalette.Text).lightness(), 200)
        self.assertNotEqual(palette.color(QPalette.Text), palette.color(QPalette.Disabled, QPalette.Text))
        self.switch(Qt.ColorScheme.Light)
        self.assertGreater(dialog.email.palette().color(QPalette.Base).lightness(), 200)

    def test_existing_icons_repaint_including_disabled_and_selected_states(self):
        existing = icon("settings")
        blue = icon("reply")
        light = existing.pixmap(24, 24).toImage()
        light_blue = blue.pixmap(24, 24).toImage()
        self.switch(Qt.ColorScheme.Dark)
        dark = existing.pixmap(24, 24).toImage()
        self.assertNotEqual(light, dark)
        self.assertNotEqual(light_blue, blue.pixmap(24, 24).toImage())
        self.assertNotEqual(dark, existing.pixmap(24, 24, QIcon.Disabled).toImage())
        self.assertNotEqual(dark, existing.pixmap(24, 24, QIcon.Selected).toImage())
        high_dpi = existing.pixmap(QSize(24, 24), 2.0)
        self.assertEqual(high_dpi.size(), QSize(48, 48))
        self.assertEqual(high_dpi.devicePixelRatio(), 2.0)
        self.switch(Qt.ColorScheme.Light)
        self.assertEqual(light, existing.pixmap(24, 24).toImage())

    def test_switch_keeps_selected_message_and_remote_images_blocked(self):
        message_id = self.db.add_message(Message(
            account_id=self.account, subject="Theme test", sender="test@example.com",
            body_html='<p style="color:#000;background:#fff">Hello</p><img src="https://example.com/tracker.png">',
        ))
        with mock.patch.object(main_window, "run_task"), \
                mock.patch("mailclient.ui.mail_watch.MailWatcher"), \
                mock.patch.object(viewer, "HAS_WEBENGINE", False):
            window = main_window.MainWindow(self.db)
            self.windows.append(window)
            window.show()
            self.app.processEvents()
            window.table.selectRow(0)
            original = self.db.message(message_id).body_html
            self.switch(Qt.ColorScheme.Dark)
            self.assertEqual(window._selected_id(), message_id)
            self.assertEqual(window.viewer._msg.id, message_id)
            self.assertFalse(window.viewer._allow_remote)
            self.assertFalse(window.viewer.banner.isHidden())
            cursor = QTextCursor(window.viewer.text.document())
            cursor.movePosition(QTextCursor.NextCharacter)
            self.assertEqual(cursor.charFormat().foreground().color(), self.app.palette().color(QPalette.Text))
            window.viewer.load_remote()
            self.switch(Qt.ColorScheme.Light)
            self.assertFalse(window.viewer._allow_remote)
            self.assertFalse(window.viewer.banner.isHidden())
            self.assertEqual(self.db.message(message_id).body_html, original)

    def test_theme_does_not_change_draft_html_or_undo_history(self):
        dialog = ComposeDialog(self.db, body="Draft text", quote_header="Original", quote_text="Quoted text")
        self.windows.append(dialog)
        dialog.show()
        dialog.editor.edit.insertPlainText("Edited ")
        original = dialog.editor.to_html()
        can_undo = dialog.editor.edit.document().isUndoAvailable()
        self.switch(Qt.ColorScheme.Dark)
        self.assertEqual(dialog.editor.to_html(), original)
        self.assertEqual(dialog.editor.edit.document().isUndoAvailable(), can_undo)
        self.switch(Qt.ColorScheme.Light)
        self.assertEqual(dialog.editor.to_html(), original)


if __name__ == "__main__":
    unittest.main()
