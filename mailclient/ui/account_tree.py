"""Дерево «ящики → папки»: ящики можно перетаскивать мышью, меняя их порядок."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem


ROLE_ID = Qt.UserRole  # (id ящика, папка) — как в главном окне


class AccountTree(QTreeWidget):
    order_changed = Signal(list)  # id ящиков в новом порядке

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def clear(self):
        # Проверка почты может перестроить дерево даже во время drag.exec().
        self._press_item = None
        self._drag_item = None
        super().clear()

    @staticmethod
    def top_of(item: QTreeWidgetItem | None) -> QTreeWidgetItem | None:
        while item is not None and item.parent() is not None:
            item = item.parent()
        return item

    def mousePressEvent(self, e):
        # запоминаем, за что взялись: щелчок по ящику сразу переводит выделение на «Входящие»
        self._press_item = self.itemAt(e.position().toPoint())
        super().mousePressEvent(e)

    def startDrag(self, actions):
        # тянуть можно только за строку ящика (не за папку)
        it = getattr(self, "_press_item", None)
        if it is None or it.parent() is not None or self.topLevelItemCount() < 2:
            return
        self._drag_item = it
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([it]))
        drag.setPixmap(self.viewport().grab(self.visualItemRect(it)))
        drag.exec(Qt.MoveAction)
        self._drag_item = None

    def _target_index(self, pos) -> int:
        """Куда вставить ящик: индекс среди ящиков (0..N)."""
        it = self.itemAt(pos)
        if it is None:
            return self.topLevelItemCount()
        top = self.top_of(it)
        idx = self.indexOfTopLevelItem(top)
        rect = self.visualItemRect(top)
        if it is top and pos.y() < rect.center().y():
            return idx
        return idx + 1

    def dragEnterEvent(self, e):
        if e.source() is self and getattr(self, "_drag_item", None) is not None:
            e.setDropAction(Qt.MoveAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is self and getattr(self, "_drag_item", None) is not None:
            super().dragMoveEvent(e)  # рисует линию-указатель
            e.setDropAction(Qt.MoveAction)
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        item = getattr(self, "_drag_item", None)
        if e.source() is not self or item is None:
            e.ignore()
            return
        ids = [self.topLevelItem(i).data(0, ROLE_ID)[0] for i in range(self.topLevelItemCount())]
        src = self.indexOfTopLevelItem(item)
        if src < 0:
            e.ignore()
            return
        dst = self._target_index(e.position().toPoint())
        e.setDropAction(Qt.IgnoreAction)  # дерево перестраивается заново, Qt ничего двигать не должен
        e.accept()
        self.setState(QAbstractItemView.NoState)
        self.viewport().update()
        if dst in (src, src + 1):
            return
        moved = ids.pop(src)
        ids.insert(dst - 1 if dst > src else dst, moved)
        self.order_changed.emit(ids)
