"""Перетаскивание вложений из окна письма в Проводник/другие программы (0.25.0)."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

from mailclient.ui import attachments_bar
from mailclient.ui.attachments_bar import AttachmentsBar

FILES = [("Predracun.pdf", b"%PDF-1"), ("con.txt", b"device"), ("a/b\\Predracun.pdf", b"second"),
         ('bad:name?.xlsx', b"x")]


class _FakeDrag:
    instances = []

    def __init__(self, source):
        self.source, self.mime, self.pixmap = source, None, None
        _FakeDrag.instances.append(self)

    def setMimeData(self, mime):
        self.mime = mime

    def setPixmap(self, pixmap):
        self.pixmap = pixmap

    def exec(self, actions, default):
        self.actions, self.default = actions, default
        # файлы должны существовать, пока идёт перетаскивание
        self.paths = [Path(url.toLocalFile()) for url in self.mime.urls()]
        self.contents = [p.read_bytes() for p in self.paths]
        return Qt.CopyAction


class AttachmentDragTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(attachments_bar.tempfile, "gettempdir", return_value=self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        _FakeDrag.instances.clear()

    def bar(self):
        bar = AttachmentsBar()
        bar.set_files(list(FILES))
        self.addCleanup(bar.deleteLater)
        return bar

    def test_drag_root_is_program_temp_folder(self):
        self.assertEqual(attachments_bar.drag_root(), Path(self.tmp.name) / "SimpleMail" / "drag")

    def test_prepare_writes_safe_unique_names_inside_drag_root(self):
        paths = attachments_bar.prepare_drag_files(FILES)
        root = attachments_bar.drag_root()
        self.assertEqual([p.read_bytes() for p in paths], [d for _, d in FILES])
        self.assertTrue(all(p.parent.parent == root for p in paths))
        self.assertEqual(len({p.parent for p in paths}), 1)
        names = [p.name for p in paths]
        self.assertEqual(names, ["Predracun.pdf", "attachment-con.txt", "Predracun (1).pdf", "bad_name_.xlsx"])

    def test_each_drag_gets_own_folder(self):
        first = attachments_bar.prepare_drag_files(FILES[:1])
        second = attachments_bar.prepare_drag_files(FILES[:1])
        self.assertNotEqual(first[0].parent, second[0].parent)
        self.assertEqual(first[0].name, second[0].name)

    def test_cleanup_removes_old_sessions_keeps_fresh(self):
        old = attachments_bar.prepare_drag_files(FILES[:1])[0].parent
        fresh = attachments_bar.prepare_drag_files(FILES[:1])[0].parent
        past = time.time() - attachments_bar.DRAG_KEEP_SECONDS - 5
        os.utime(old, (past, past))
        attachments_bar.cleanup_drag_files()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        attachments_bar.cleanup_drag_files(0)
        self.assertFalse(fresh.exists())
        self.assertFalse((Path(self.tmp.name) / "SimpleMail").exists())  # пустые папки не остаются

    def test_cleanup_without_folder_is_harmless(self):
        attachments_bar.cleanup_drag_files(0)
        self.assertFalse((Path(self.tmp.name) / "SimpleMail").exists())

    def test_list_allows_dragging_out_in_both_modes(self):
        bar = self.bar()
        for mode in ("icons", "list"):
            bar.set_mode(mode)
            self.assertTrue(bar.list.dragEnabled(), mode)
            self.assertEqual(bar.list.dragDropMode(), QAbstractItemView.DragOnly)
            self.assertEqual(bar.list.defaultDropAction(), Qt.CopyAction)
            self.assertFalse(bar.list.viewport().acceptDrops())  # обратно в список ничего не бросить

    def test_drag_selected_attachments_as_files(self):
        bar = self.bar()
        messages = []
        bar.saved.connect(messages.append)
        bar.list.item(0).setSelected(True)
        bar.list.item(2).setSelected(True)
        with mock.patch.object(attachments_bar, "QDrag", _FakeDrag):
            bar.list.startDrag(Qt.CopyAction | Qt.MoveAction)
        drag = _FakeDrag.instances[-1]
        self.assertEqual(drag.actions, Qt.CopyAction)  # только копирование
        self.assertEqual(drag.contents, [b"%PDF-1", b"second"])
        self.assertEqual([p.name for p in drag.paths], ["Predracun.pdf", "Predracun (1).pdf"])
        self.assertTrue(all(attachments_bar.drag_root() in p.parents for p in drag.paths))
        self.assertTrue(all(p.exists() for p in drag.paths))  # Проводник может копировать после отпускания
        self.assertEqual(messages, ["Скопировано вложений: 2"])

    def test_nothing_selected_no_drag(self):
        bar = self.bar()
        with mock.patch.object(attachments_bar, "QDrag", _FakeDrag):
            self.assertEqual(bar.start_drag(), Qt.IgnoreAction)
        self.assertEqual(_FakeDrag.instances, [])
        self.assertFalse(attachments_bar.drag_root().exists())

    def test_write_error_reported_and_nothing_left(self):
        bar = self.bar()
        messages = []
        bar.saved.connect(messages.append)
        bar.list.item(0).setSelected(True)
        with mock.patch.object(attachments_bar, "QDrag", _FakeDrag), \
                mock.patch.object(attachments_bar, "mark_from_internet", side_effect=OSError("диск полон")):
            self.assertEqual(bar.start_drag(), Qt.IgnoreAction)
        self.assertEqual(_FakeDrag.instances, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("диск полон", messages[0])
        self.assertEqual(list(attachments_bar.drag_root().iterdir()), [])

    def test_quit_removes_all_drag_files(self):
        bar = self.bar()
        bar.list.item(1).setSelected(True)
        with mock.patch.object(attachments_bar, "QDrag", _FakeDrag):
            bar.start_drag()
        self.assertTrue(attachments_bar.drag_root().exists())
        self.app.aboutToQuit.emit()
        self.assertFalse(attachments_bar.drag_root().exists())


if __name__ == "__main__":
    unittest.main()
