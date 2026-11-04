"""Панель вложений в окне письма: плашки файлов (значок, имя, размер).

Двойной клик — открыть файл программой по умолчанию; правый клик — меню;
кнопки «Сохранить» (выбранные) и «Сохранить все».
"""
from __future__ import annotations

from ..i18n import tr

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
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


_sys_icons: dict[str, object] = {}


def system_file_icon(name: str):
    """Значок типа файла из системы (как в Проводнике). Для этого создаём пустой файл-образец
    с таким же расширением во временной папке — Windows отдаёт значок по реальному файлу."""
    ext = Path(name).suffix.lower()
    if ext in _sys_icons:
        return _sys_icons[ext]
    ic = None
    try:
        from PySide6.QtCore import QFileInfo
        from PySide6.QtWidgets import QFileIconProvider
        folder = Path(tempfile.gettempdir()) / "SimpleMail" / "icon-samples"
        folder.mkdir(parents=True, exist_ok=True)
        sample = folder / f"sample{ext or '.bin'}"
        if not sample.exists():
            sample.write_bytes(b"")
        ic = QFileIconProvider().icon(QFileInfo(str(sample)))
        if ic.isNull() or not ic.availableSizes():
            ic = None
    except Exception:
        ic = None
    _sys_icons[ext] = ic
    return ic


def file_icon(name: str):
    if os.name == "nt":  # на Windows — настоящие значки программ
        ic = system_file_icon(name)
        if ic is not None:
            return ic
    ext = Path(name).suffix.lower().lstrip(".")
    for icon_name, exts in TYPE_ICONS.items():
        if ext in exts:
            return icon(icon_name, TYPE_COLORS[icon_name])
    return icon("file", "#6b7280")


def human_size(n: int) -> str:
    if n >= 1048576:
        return tr("{0:.1f} МБ", n / 1048576).replace(".", ",")
    return tr("{0} КБ", max(1, round(n / 1024)))


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
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setStyleSheet("QListWidget{border:1px solid #e5e7eb;background:palette(base);}"
                                "QListWidget::item{padding:0px 2px;}")
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.list.itemDoubleClicked.connect(lambda it: self._open(self.list.row(it)))
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)

        self.toggle = QToolButton(); self.toggle.setAutoRaise(True); self.toggle.setCheckable(True)
        self.toggle.setToolTip(tr("Свернуть / развернуть список вложений"))
        self.toggle.toggled.connect(self._set_collapsed)
        self.title = QLabel()
        self.title.setStyleSheet("color:#374151")
        self.title.mousePressEvent = lambda e: self.toggle.toggle()

        self.mode_btn = QToolButton(); self.mode_btn.setAutoRaise(True)
        self.mode_btn.setToolTip(tr("Вид: список / значки"))
        self.mode_btn.clicked.connect(lambda: self.set_mode("icons" if self.mode == "list" else "list"))
        save = QToolButton(); save.setText(tr("Сохранить")); save.setIcon(icon("download"))
        save.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); save.setToolTip(tr("Сохранить выбранные вложения"))
        save.clicked.connect(self._save_selected)
        save_all = QToolButton(); save_all.setText(tr("Сохранить все")); save_all.setIcon(icon("folder-open"))
        save_all.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); save_all.setToolTip(tr("Сохранить все вложения в папку"))
        save_all.clicked.connect(self.save_all)

        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0); top.setSpacing(2)
        top.addWidget(self.toggle); top.addWidget(self.title, 1)
        top.addWidget(self.mode_btn); top.addWidget(save); top.addWidget(save_all)
        lay = QVBoxLayout(self); lay.setContentsMargins(6, 2, 6, 2); lay.setSpacing(2)
        lay.addLayout(top); lay.addWidget(self.list)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.mode = "list"
        self.set_mode("list")
        self._set_collapsed(False)
        self.hide()

    # ---------- вид ----------
    mode_changed = Signal(str)
    collapsed_changed = Signal(bool)

    MAX_LINES = 3       # в режиме списка: строк без прокрутки
    MAX_ICON_ROWS = 2   # в режиме значков: рядов без прокрутки

    def set_mode(self, mode: str):
        """list — плотные плашки «значок имя (размер)» в несколько колонок; icons — крупные значки сеткой."""
        self.mode = mode
        self.list.setViewMode(QListView.IconMode if mode == "icons" else QListView.ListMode)
        self.list.setFlow(QListView.LeftToRight)
        self.list.setWrapping(True)
        self.list.setMovement(QListView.Static)
        self.list.setResizeMode(QListView.Adjust)
        if mode == "icons":
            self.list.setIconSize(QSize(40, 40))
            self.list.setGridSize(QSize(120, 66))
            self.list.setSpacing(0)
            self.list.setWordWrap(False)  # имя в одну строку, середина сокращается «…»
            self.list.setTextElideMode(Qt.ElideMiddle)
            self.mode_btn.setIcon(icon("list", "#374151"))
            self.mode_btn.setToolTip(tr("Показать плотным списком"))
        else:
            self.list.setIconSize(QSize(16, 16))
            self.list.setGridSize(QSize())
            self.list.setSpacing(1)
            self.list.setWordWrap(False)
            self.mode_btn.setIcon(icon("image", "#374151"))
            self.mode_btn.setToolTip(tr("Показать крупными значками"))
        self._fill()
        self.mode_changed.emit(mode)

    def _set_collapsed(self, collapsed: bool):
        self.toggle.setArrowType(Qt.RightArrow if collapsed else Qt.DownArrow)
        self.list.setVisible(not collapsed)
        self._fit_height()
        self.collapsed_changed.emit(collapsed)

    # ---------- высота по содержимому ----------
    def _item_size(self, text: str) -> QSize:
        fm = self.list.fontMetrics()
        h = max(fm.height(), 16) + 4
        return QSize(min(16 + 6 + fm.horizontalAdvance(text) + 12, 360), h)

    def _content_height(self, width: int) -> int:
        """Высота списка, при которой видны все плашки (но не больше MAX_LINES/MAX_ICON_ROWS рядов)."""
        n = self.list.count()
        if not n:
            return 0
        frame = 2 * self.list.frameWidth()
        width = max(width - frame - 4, 50)
        if self.mode == "icons":
            per_row = max(1, width // 120)
            rows = min(-(-n // per_row), self.MAX_ICON_ROWS)
            return rows * 66 + frame + 4
        sp = self.list.spacing()
        lines, x = 1, sp
        for i in range(n):
            w = self.list.item(i).sizeHint().width() + sp
            if x + w > width and x > sp:
                lines, x = lines + 1, sp
            x += w
        row_h = self.list.item(0).sizeHint().height() + sp
        return min(lines, self.MAX_LINES) * row_h + sp + frame + 2

    def _fit_height(self):
        m = self.layout().contentsMargins() if self.layout() else None
        width = self.width() - (m.left() + m.right() if m else 0)
        self.list.setFixedHeight(max(self._content_height(width), 20))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_height()

    def is_collapsed(self) -> bool:
        return self.toggle.isChecked()

    # ---------- данные ----------
    def set_files(self, files: list[tuple[str, bytes]], default_dir: str = ""):
        self._files = files
        self.default_dir = default_dir
        self._fill()
        total = sum(len(d) for _, d in files)
        self.title.setText(tr("<b>Вложения: {0}</b> ({1})", len(files), human_size(total)) if files else "")
        self.setVisible(bool(files))

    def _fill(self):
        self.list.clear()
        for name, data in self._files:
            text = name if self.mode == "icons" else f"{name} ({human_size(len(data))})"
            it = QListWidgetItem(file_icon(name), text)
            it.setToolTip(tr("{0}\n{1}\nДвойной клик — открыть, правый клик — меню", name, human_size(len(data))))
            it.setSizeHint(QSize(116, 62) if self.mode == "icons" else self._item_size(text))
            self.list.addItem(it)
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
            if QMessageBox.warning(self, tr("Осторожно"), tr("«{0}» — исполняемый файл. Он может навредить компьютеру.\nОткрыть всё равно?", name), QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _save_one(self, row: int):
        name, data = self._files[row]
        start = str(Path(self.default_dir) / Path(name).name) if self.default_dir else Path(name).name
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить вложение"), start)
        if path:
            Path(path).write_bytes(data)
            self.saved.emit(tr("Сохранено: {0}", path))

    def _save_rows(self, rows: list[int]):
        if len(rows) == 1:
            self._save_one(rows[0])
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Куда сохранить вложения"), self.default_dir)
        if not folder:
            return
        for r in rows:
            name, data = self._files[r]
            unique_path(Path(folder), name).write_bytes(data)
        self.saved.emit(tr("Сохранено вложений: {0} в {1}", len(rows), folder))

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
            menu.addAction(icon("external-link"), tr("Открыть"), lambda: self._open(row))
            menu.addAction(icon("download"), tr("Сохранить как…"), lambda: self._save_one(row))
            if len(self._selected_rows()) > 1:
                menu.addAction(tr("Сохранить выбранные…"), self._save_selected)
            menu.addSeparator()
        menu.addAction(icon("folder-open"), tr("Сохранить все…"), self.save_all)
        if self.default_dir:
            menu.addAction(tr("Открыть папку вложений"), self._open_folder)
        menu.exec(self.list.mapToGlobal(pos))
