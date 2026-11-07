"""Список писем с колонкой флажков выбора.

Флажок в строке показывает, выделено ли письмо, а щелчок по нему добавляет письмо к выделению
или убирает его (как Ctrl+щелчок). Флажок в заголовке колонки выделяет все письма или снимает
выделение. Отдельного состояния «отмечено» нет: все действия по-прежнему работают с выделением.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (QApplication, QHeaderView, QStyle, QStyledItemDelegate, QStyleOptionButton,
                               QStyleOptionViewItem, QTableWidget)

from .theme import is_dark

ROLE_UNREAD = Qt.UserRole + 2
UNREAD_TEXT_DARK = QColor("#ffd180")

# Отступ от края колонки, в котором щелчок по заголовку остаётся изменением ширины соседней колонки.
_EDGE = 4


def _indicator_size(style, widget) -> QSize:
    return QSize(style.pixelMetric(QStyle.PM_IndicatorWidth, None, widget),
                 style.pixelMetric(QStyle.PM_IndicatorHeight, None, widget))


class MessageDelegate(QStyledItemDelegate):
    """Выделяет непрочитанные письма в тёмной теме, в том числе при выборе строки."""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.data(ROLE_UNREAD) and is_dark():
            option.palette.setColor(QPalette.Text, UNREAD_TEXT_DARK)
            option.palette.setColor(QPalette.HighlightedText, UNREAD_TEXT_DARK)


class SelectionCheckDelegate(QStyledItemDelegate):
    """Рисует в ячейке флажок по центру: включён, если строка выделена."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, widget)
        check = QStyleOptionViewItem(opt)
        check.rect = QStyle.alignedRect(opt.direction, Qt.AlignCenter, _indicator_size(style, widget), opt.rect)
        selected = bool(opt.state & QStyle.State_Selected)
        check.state = (opt.state & ~QStyle.State_HasFocus & ~QStyle.State_Selected) | (
            QStyle.State_On if selected else QStyle.State_Off)
        style.drawPrimitive(QStyle.PE_IndicatorItemViewItemCheck, check, painter, widget)


class CheckHeader(QHeaderView):
    """Заголовок списка: в колонке флажков — флажок «выделить все / снять выделение»."""

    toggle_all = Signal()

    def __init__(self, check_column: int, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.check_column = check_column
        self.check_state = lambda: Qt.Unchecked
        self._pressed_check = False

    def _on_check(self, pos) -> bool:
        section = self.logicalIndexAt(pos)
        if section != self.check_column:
            return False
        start = self.sectionViewportPosition(section)
        return start + _EDGE <= pos.x() <= start + self.sectionSize(section) - _EDGE

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._on_check(event.position().toPoint()):
            self._pressed_check = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed_check and event.button() == Qt.LeftButton:
            self._pressed_check = False
            event.accept()
            if self._on_check(event.position().toPoint()):
                self.toggle_all.emit()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._on_check(event.position().toPoint()):
            event.accept()  # второй щелчок двойного — тоже переключение, а не сортировка
            self.toggle_all.emit()
            return
        super().mouseDoubleClickEvent(event)

    def paintSection(self, painter, rect: QRect, logical_index: int):
        painter.save()
        super().paintSection(painter, rect, logical_index)
        painter.restore()
        if logical_index != self.check_column or not rect.isValid():
            return
        opt = QStyleOptionButton()
        opt.rect = QStyle.alignedRect(Qt.LeftToRight, Qt.AlignCenter, _indicator_size(self.style(), self), rect)
        state = self.check_state()
        opt.state = QStyle.State_Enabled | {Qt.Checked: QStyle.State_On,
                                            Qt.PartiallyChecked: QStyle.State_NoChange}.get(state, QStyle.State_Off)
        self.style().drawPrimitive(QStyle.PE_IndicatorCheckBox, opt, painter, self)


class MessageTable(QTableWidget):
    """Таблица писем: щелчок по флажку строки переключает её выделение, не трогая остальные."""

    def __init__(self, rows: int, columns: int, check_column: int, parent=None):
        super().__init__(rows, columns, parent)
        self.check_column = check_column
        self.check_header = CheckHeader(check_column, self)
        self.setHorizontalHeader(self.check_header)
        self.check_header.check_state = self.selection_state
        self.check_header.toggle_all.connect(self.toggle_all)
        self.setItemDelegate(MessageDelegate(self))
        self.setItemDelegateForColumn(check_column, SelectionCheckDelegate(self))
        self._selected_rows = 0
        self.selectionModel().selectionChanged.connect(self._count_selected)
        for signal in (self.model().rowsInserted, self.model().rowsRemoved, self.model().modelReset):
            signal.connect(self._count_selected)

    def _count_selected(self, *_):
        self._selected_rows = len(self.selectionModel().selectedRows())
        self.check_header.updateSection(self.check_column)

    def selectionCommand(self, index, event=None):
        if index.isValid() and index.column() == self.check_column and event is not None:
            if event.type() == QEvent.MouseButtonPress:
                return QItemSelectionModel.Toggle | QItemSelectionModel.Rows
            if event.type() in (QEvent.MouseMove, QEvent.MouseButtonRelease):
                return QItemSelectionModel.NoUpdate  # протаскивание по флажкам не выделяет диапазон
        return super().selectionCommand(index, event)

    def selection_state(self):
        rows, selected = self.rowCount(), self._selected_rows
        if not rows or not selected:
            return Qt.Unchecked
        return Qt.Checked if selected >= rows else Qt.PartiallyChecked

    def toggle_all(self):
        """Флажок в заголовке: выделить все письма, а если выделены все — снять выделение."""
        self.setFocus()
        if self.rowCount() and self.selection_state() == Qt.Checked:
            self.clearSelection()
        else:
            self.selectAll()
