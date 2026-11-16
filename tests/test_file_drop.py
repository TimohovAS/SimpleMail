"""Вложения перетаскиванием файлов из Проводника в окно письма."""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication

from mailclient.db import Account, Database
from mailclient.ui.compose_dialog import ComposeDialog
from mailclient.ui.file_drop import ZONE_ATTACH, ZONE_INLINE, dropped_files

app = QApplication.instance() or QApplication([])
ACTIONS = Qt.CopyAction | Qt.MoveAction | Qt.LinkAction


def mime_for(*paths, extra_urls=()):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths] + [QUrl(u) for u in extra_urls])
    return mime


class FileDropTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = Database(root / "t.db")
        self.addCleanup(self.db.close)
        self.acc = self.db.account(self.db.save_account(Account(
            name="Я", email="me@example.com", username="me", imap_host="i", smtp_host="s")))
        self.pdf = root / "Счёт.pdf"; self.pdf.write_bytes(b"%PDF-1.4 test")
        self.txt = root / "notes.txt"; self.txt.write_bytes(b"hello")
        self.png = root / "photo.png"
        image = QImage(20, 10, QImage.Format_RGB32); image.fill(Qt.red); image.save(str(self.png))
        self.folder = root / "Папка"; self.folder.mkdir()
        self.dlg = ComposeDialog(self.db, account_id=self.acc.id, to="a@example.com", subject="Тема")
        self.dlg.resize(900, 700)
        self.dlg.show()
        app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.dlg._autosave.stop()
        self.dlg._dirty = False
        self.dlg.done(0)
        self.dlg.deleteLater()
        app.processEvents()

    def center(self, widget):
        return QPoint(widget.width() // 2, widget.height() // 2)

    def drag(self, widget, mime, pos=None, drop=True):
        """Полный жест: вход, движение, бросок над widget. Возвращает событие броска."""
        pos = pos or self.center(widget)
        enter = QDragEnterEvent(pos, ACTIONS, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(widget, enter)
        move = QDragMoveEvent(pos, ACTIONS, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(widget, move)
        self.last_move = move
        if not drop:
            return enter
        event = QDropEvent(QPointF(pos), ACTIONS, mime, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(widget, event)
        app.processEvents()
        return event

    def names(self):
        return [name for name, _ in self.dlg.attachments]

    def test_files_dropped_on_any_field_become_attachments(self):
        targets = [self.dlg.to, self.dlg.subject, self.dlg.editor.edit.viewport(), self.dlg]
        for target in targets:
            self.drag(target, mime_for(self.txt))
        self.assertEqual(self.names(), ["notes.txt"] * len(targets))
        self.assertEqual(self.dlg.to.text(), "a@example.com")
        self.assertEqual(self.dlg.subject.text(), "Тема")
        self.assertNotIn("notes.txt", self.dlg.editor.to_plain())
        self.assertTrue(self.dlg.att_list.isVisibleTo(self.dlg))

    def test_several_files_and_contents(self):
        event = self.drag(self.dlg.editor.edit.viewport(), mime_for(self.pdf, self.txt))
        self.assertTrue(event.isAccepted())
        self.assertEqual(event.dropAction(), Qt.CopyAction)  # Проводник не удалит исходный файл
        self.assertEqual(self.dlg.attachments, [("Счёт.pdf", b"%PDF-1.4 test"), ("notes.txt", b"hello")])
        self.assertEqual(self.dlg.att_list.count(), 2)

    def test_image_dropped_into_text_is_inserted_inline(self):
        self.drag(self.dlg.editor.edit.viewport(), mime_for(self.png))
        self.assertEqual(self.dlg.attachments, [])
        images = self.dlg.editor.inline_images()
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][1], "image/png")

    def test_image_dropped_outside_text_is_attached(self):
        self.drag(self.dlg.subject, mime_for(self.png))
        self.assertEqual(self.names(), ["photo.png"])
        self.assertEqual(self.dlg.editor.inline_images(), [])

    def test_image_with_other_file_is_attached_everywhere(self):
        self.drag(self.dlg.editor.edit.viewport(), mime_for(self.png, self.pdf))
        self.assertEqual(self.names(), ["photo.png", "Счёт.pdf"])
        self.assertEqual(self.dlg.editor.inline_images(), [])

    def test_plain_text_mode_attaches_images(self):
        self.dlg.mode.setCurrentIndex(1)
        self.drag(self.dlg.editor.edit.viewport(), mime_for(self.png))
        self.assertEqual(self.names(), ["photo.png"])

    def test_overlay_zones_follow_cursor_and_hide(self):
        overlay = self.dlg._drop_overlay
        self.drag(self.dlg.subject, mime_for(self.pdf), drop=False)
        self.assertTrue(overlay.isVisible())
        self.assertEqual(overlay.zone, ZONE_ATTACH)
        self.assertIsNone(overlay.inline_rect)
        self.assertEqual(overlay.geometry(), self.dlg.rect())
        overlay.grab()  # рисование без ошибок
        QApplication.sendEvent(self.dlg.subject, QDragLeaveEvent())
        self.assertFalse(overlay.isVisible())

        self.drag(self.dlg.editor.edit.viewport(), mime_for(self.png), drop=False)
        self.assertTrue(overlay.isVisible())
        self.assertEqual(overlay.zone, ZONE_INLINE)
        self.assertIsNotNone(overlay.inline_rect)
        overlay.grab()
        self.drag(self.dlg.to, mime_for(self.png), drop=False)
        self.assertEqual(overlay.zone, ZONE_ATTACH)
        self.assertIsNotNone(overlay.inline_rect)  # зона «в текст» видна, но не выбрана
        mime = mime_for(self.png)  # событие не владеет данными — держим их сами
        QApplication.sendEvent(self.dlg.to, QDropEvent(QPointF(5, 5), ACTIONS, mime, Qt.LeftButton, Qt.NoModifier))
        app.processEvents()
        self.assertFalse(overlay.isVisible())

    def test_folders_and_web_links_are_not_taken(self):
        enter = self.drag(self.dlg.subject, mime_for(self.folder), drop=False)
        self.assertFalse(enter.isAccepted())
        self.assertFalse(self.dlg._drop_overlay.isVisible())
        self.assertEqual(dropped_files(mime_for(self.folder, extra_urls=["https://example.com/a.pdf"])), [])
        self.assertEqual(dropped_files(mime_for(self.folder, self.pdf)), [self.pdf])
        self.drag(self.dlg.editor.edit.viewport(), mime_for(self.folder, self.pdf))
        self.assertEqual(self.names(), ["Счёт.pdf"])

    def test_ordinary_text_drag_still_works(self):
        mime = QMimeData(); mime.setText("b@example.com")
        self.drag(self.dlg.subject, mime)
        self.assertEqual(self.dlg.attachments, [])
        self.assertFalse(self.dlg._drop_overlay.isVisible())

    def test_unreadable_file_reports_and_keeps_others(self):
        from unittest import mock
        from PySide6.QtWidgets import QMessageBox
        missing = Path(self.tmp.name) / "gone.bin"
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.dlg._add_files([str(missing), str(self.txt)])
        self.assertEqual(self.names(), ["notes.txt"])
        warning.assert_called_once()
        self.assertIn("gone.bin", warning.call_args.args[2])

    def test_dropped_attachment_is_saved_with_draft(self):
        self.drag(self.dlg, mime_for(self.pdf))
        self.dlg._autosave.stop()
        self.dlg._save_draft()
        self.assertEqual(self.db.message_attachments(self.dlg._draft_id), [("Счёт.pdf", b"%PDF-1.4 test")])


if __name__ == "__main__":
    unittest.main()
