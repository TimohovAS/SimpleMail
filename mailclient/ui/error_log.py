"""Окно «Журнал ошибок»: ошибки фоновых действий этого сеанса и файл журнала.

Раньше такие ошибки (пометка прочитанным, перемещение, загрузка вложений) показывались
15 секунд в строке состояния и легко терялись.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QSplitter, QVBoxLayout,
)

from ..errors import ErrorInfo
from ..i18n import tr

MAX_ENTRIES = 200


@dataclass
class ErrorEntry:
    when: float
    title: str
    error: ErrorInfo

    def report(self) -> str:
        from .messages import error_report
        return time.strftime("%d.%m.%Y %H:%M:%S ", time.localtime(self.when)) + error_report(self.title, self.error)


class ErrorLog:
    """Ошибки текущего сеанса; unseen — сколько ещё не просмотрено (для значка в строке состояния)."""

    def __init__(self):
        self.entries: list[ErrorEntry] = []
        self.unseen = 0

    def add(self, title: str, error: ErrorInfo) -> ErrorEntry:
        entry = ErrorEntry(time.time(), title, error)
        self.entries.append(entry)
        del self.entries[:-MAX_ENTRIES]
        self.unseen += 1
        return entry

    def mark_seen(self):
        self.unseen = 0


def tail(path: Path, limit: int = 200_000) -> str:
    """Конец файла журнала (файл может быть большим)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return text if size <= limit else text.split("\n", 1)[-1]


class ErrorLogDialog(QDialog):
    def __init__(self, log: ErrorLog, log_file: Path, parent=None):
        super().__init__(parent)
        self.log, self.log_file = log, Path(log_file)
        self.setWindowTitle(tr("Журнал ошибок"))
        self.resize(820, 520)
        self.list = QListWidget()
        for entry in reversed(log.entries):
            stamp = time.strftime("%H:%M:%S", time.localtime(entry.when))
            item = QListWidgetItem(f"{stamp}  {entry.title}: {entry.error.summary}")
            item.setData(Qt.UserRole, entry)
            self.list.addItem(item)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.list.currentItemChanged.connect(self._show_entry)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.list)
        split.addWidget(self.details)
        empty = QLabel(tr("В этом сеансе ошибок не было. Ниже — конец файла журнала."))
        empty.setVisible(not log.entries)
        self.copy_btn = QPushButton(tr("Скопировать подробности"))
        self.copy_btn.clicked.connect(self._copy)
        file_btn = QPushButton(tr("Показать файл журнала"))
        file_btn.clicked.connect(self._show_file)
        folder_btn = QPushButton(tr("Открыть папку журнала"))
        folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_file.parent))))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        for widget in (self.copy_btn, file_btn, folder_btn):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(buttons)
        lay = QVBoxLayout(self)
        lay.addWidget(empty)
        lay.addWidget(split, 1)
        lay.addLayout(row)
        if log.entries:
            self.list.setCurrentRow(0)
        else:
            self.list.hide()
            self._show_file()
        log.mark_seen()

    def _show_entry(self, item, _previous=None):
        if item is not None:
            self.details.setPlainText(item.data(Qt.UserRole).report())

    def _show_file(self):
        self.list.clearSelection()
        text = tail(self.log_file)
        self.details.setPlainText(text or tr("Файл журнала пуст: {0}", str(self.log_file)))
        self.details.moveCursor(self.details.textCursor().MoveOperation.End)

    def _copy(self):
        QApplication.clipboard().setText(self.details.toPlainText())
        self.copy_btn.setText(tr("Скопировано"))
