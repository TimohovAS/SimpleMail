"""Панель вложений в окне письма: плашки файлов (значок, имя, размер).

Двойной клик — открыть файл программой по умолчанию; правый клик — меню;
кнопки «Сохранить» (выбранные) и «Сохранить все».
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QToolButton, QVBoxLayout, QWidget,
)

from .icons import icon

TYPE_ICONS = {
    "file-image": {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "svg", "heic"},
    "file-text": {"pdf", "doc", "docx", "odt", "rtf", "txt", "md"},
    "file-spreadsheet": {"xls", "xlsx", "ods", "csv"},
    "file-archive": {"zip", "rar", "7z", "gz", "tar", "bz2"},
    "file-audio": {"mp3", "wav", "ogg", "m4a", "flac"},
    "file-video": {"mp4", "avi", "mkv", "mov", "wmv"},
    "file-code": {"html", "htm", "xml", "json", "py", "js"},
}
TYPE_COLORS = {"file-image": "#9333ea", "file-text": "#dc2626", "file-spreadsheet": "#16a34a",
               "file-archive": "#ca8a04", "file-audio": "#0891b2", "file-video": "#db2777", "file-code": "#2563eb"}


def file_icon(name: str):
    ext = Path(name).suffix.lower().lstrip(".")
    for icon_name, exts in TYPE_ICONS.items():
        if ext in exts:
            return icon(icon_name, TYPE_COLORS[icon_name])
    return icon("file", "#6b7280")


def human_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} МБ".replace(".", ",")
    return f"{max(1, round(n / 1024))} КБ"


def unique_path(folder: Path, name: str) -> Path:
    name = Path(name).name or "attachment"
    target, n = folder / name, 1
    while target.exists():
        target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return target


class AttachmentsBar(QWidget):
    saved = Signal(str)  # сообщение для строки статуса

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files: list[tuple[str, bytes]] = []
        self.default_dir = ""  # папка «Вложения» ящика

        self.list = QListWidget()
        self.list.setViewMode(QListView.ListMode)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(True)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setSpacing(3)
        self.list.setIconSize(QSize(24, 24))
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setStyleSheet("QListWidget{border:none;background:transparent;}"
                                "QListWidget::item{border:1px solid #d1d5db;border-radius:4px;padding:2px 6px;}"
                                "QListWidget::item:selected{border-color:#2563eb;background:#dbeafe;color:black;}")
        self.list.itemDoubleClicked.connect(lambda it: self._open(self.list.row(it)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)

        self.title = QLabel()
        self.title.setStyleSheet("color:#374151")
        save = QToolButton(); save.setText("Сохранить"); save.setIcon(icon("download"))
        save.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); save.setToolTip("Сохранить выбранные вложения")
        save.clicked.connect(self._save_selected)
        save_all = QToolButton(); save_all.setText("Сохранить все"); save_all.setIcon(icon("folder-open"))
        save_all.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); save_all.setToolTip("Сохранить все вложения в папку")
        save_all.clicked.connect(self.save_all)

        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.title, 1); top.addWidget(save); top.addWidget(save_all)
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 2, 8, 4); lay.setSpacing(2)
        lay.addLayout(top); lay.addWidget(self.list)
        self.hide()

    # ---------- данные ----------
    def set_files(self, files: list[tuple[str, bytes]], default_dir: str = ""):
        self._files = files
        self.default_dir = default_dir
        self.list.clear()
        for name, data in files:
            it = QListWidgetItem(file_icon(name), f"{name}  ({human_size(len(data))})")
            it.setToolTip(f"{name}\n{human_size(len(data))}\nДвойной клик — открыть, правый клик — меню")
            self.list.addItem(it)
        total = sum(len(d) for _, d in files)
        self.title.setText(f"<b>Вложения: {len(files)}</b> ({human_size(total)})" if files else "")
        self.setVisible(bool(files))
        self._fit_height()

    def _fit_height(self):
        """Высота по числу строк плашек (считаем по реальной ширине панели)."""
        if not self._files:
            return
        width = max(200, self.list.viewport().width() or self.width() - 16)
        rows, x = 1, 0
        for i in range(self.list.count()):
            w = self.list.visualItemRect(self.list.item(i)).width() or 180
            w += 6
            if x and x + w > width:
                rows, x = rows + 1, 0
            x += w
        self.list.setFixedHeight(min(rows, 3) * 36 + 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_height()

    # ---------- действия ----------
    def _selected_rows(self) -> list[int]:
        return sorted(self.list.row(it) for it in self.list.selectedItems())

    def _open(self, row: int):
        name, data = self._files[row]
        folder = Path(tempfile.gettempdir()) / "SimpleMail"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / Path(name).name
        try:
            path.write_bytes(data)
        except OSError:  # файл уже открыт другой программой — пишем рядом под другим именем
            path = unique_path(folder, name)
            path.write_bytes(data)
        if Path(name).suffix.lower() in (".exe", ".bat", ".cmd", ".js", ".vbs", ".scr", ".ps1", ".msi", ".com"):
            if QMessageBox.warning(self, "Осторожно", f"«{name}» — исполняемый файл. Он может навредить компьютеру.\n"
                                   "Открыть всё равно?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _save_one(self, row: int):
        name, data = self._files[row]
        start = str(Path(self.default_dir) / Path(name).name) if self.default_dir else Path(name).name
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить вложение", start)
        if path:
            Path(path).write_bytes(data)
            self.saved.emit(f"Сохранено: {path}")

    def _save_rows(self, rows: list[int]):
        if len(rows) == 1:
            self._save_one(rows[0])
            return
        folder = QFileDialog.getExistingDirectory(self, "Куда сохранить вложения", self.default_dir)
        if not folder:
            return
        for r in rows:
            name, data = self._files[r]
            unique_path(Path(folder), name).write_bytes(data)
        self.saved.emit(f"Сохранено вложений: {len(rows)} в {folder}")

    def _save_selected(self):
        rows = self._selected_rows() or list(range(len(self._files)))
        self._save_rows(rows)

    def save_all(self):
        if self._files:
            self._save_rows(list(range(len(self._files))) if len(self._files) > 1 else [0])

    def _open_folder(self):
        if self.default_dir:
            Path(self.default_dir).mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.default_dir))

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        menu = QMenu(self)
        if it:
            row = self.list.row(it)
            menu.addAction(icon("external-link"), "Открыть", lambda: self._open(row))
            menu.addAction(icon("download"), "Сохранить как…", lambda: self._save_one(row))
            if len(self._selected_rows()) > 1:
                menu.addAction("Сохранить выбранные…", self._save_selected)
            menu.addSeparator()
        menu.addAction(icon("folder-open"), "Сохранить все…", self.save_all)
        if self.default_dir:
            menu.addAction("Открыть папку вложений", self._open_folder)
        menu.exec(self.list.mapToGlobal(pos))
