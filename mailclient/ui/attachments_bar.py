"""Панель вложений в окне письма: плашки файлов (значок, имя, размер).

Двойной клик — открыть файл программой по умолчанию; правый клик — меню;
кнопки «Сохранить» (выбранные) и «Сохранить все». Плашки (одну или несколько выбранных)
можно перетащить в Проводник, на рабочий стол или в другую программу — туда копируются файлы.
"""
from __future__ import annotations

from ..i18n import tr

import os
import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QMimeData, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDrag
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMenu,
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
        with tempfile.TemporaryDirectory(prefix="SimpleMail-icon-") as directory:
            sample = Path(directory) / f"sample{ext or '.bin'}"
            with sample.open("xb"):
                pass
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


# Файлы, которые Windows запускает как программы, скрипты или ярлыки, и документы с макросами.
DANGEROUS_EXTENSIONS = {
    ".html", ".htm", ".svg", ".svgz", ".xhtml",
    ".exe", ".com", ".scr", ".pif", ".bat", ".cmd", ".msi", ".msp", ".msc", ".cpl", ".dll", ".sys",
    ".js", ".jse", ".vbs", ".vbe", ".wsf", ".wsh", ".ps1", ".psm1", ".hta", ".jar", ".reg",
    ".lnk", ".url", ".scf", ".inf", ".chm", ".iso", ".img", ".vhd", ".vhdx", ".appx", ".msix",
    ".application", ".gadget", ".docm", ".dotm", ".xlsm", ".xltm", ".xlam", ".pptm", ".ppsm", ".one",
}


def is_dangerous(name: str) -> bool:
    """«счёт.pdf.exe », «x.LNK» и т.п.: Windows отбрасывает конечные точки и пробелы."""
    return Path(name.rstrip(" .")).suffix.lower() in DANGEROUS_EXTENSIONS


def mark_from_internet(path) -> None:
    """Пометка «получено из интернета» (Zone.Identifier): Windows и Office откроют файл
    в защищённом режиме и предупредят перед запуском."""
    import os
    if os.name != "nt":
        return
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as stream:
            stream.write("[ZoneTransfer]\r\nZoneId=3\r\n")
    except OSError:
        pass


def unique_path(folder: Path, name: str) -> Path:
    name = Path(name).name or "attachment"
    target, n = folder / name, 1
    while target.exists():
        target = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return target


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    """Имя вложения, которое можно создать в Windows: без пути, запрещённых знаков и имён устройств."""
    filename = Path(name.replace("\\", "/")).name.rstrip(" .") or "attachment"
    # Windows трактует двоеточие как альтернативный поток файла.
    filename = "".join("_" if char in '<>:"|?*' or ord(char) < 32 else char for char in filename)
    if filename.split(".")[0].upper() in _RESERVED_NAMES:
        filename = "attachment-" + filename
    return filename


def temporary_attachment(name: str, data: bytes) -> Path:
    # Каждый открытый файл получает собственный непредсказуемый каталог.
    folder = Path(tempfile.mkdtemp(prefix="SimpleMail-attachment-"))
    path = folder / safe_filename(name)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except Exception:
        try:
            path.unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            pass
        raise
    return path


# ---------- файлы для перетаскивания ----------
# Проводник копирует перетащенные файлы уже после отпускания кнопки мыши (большие — в фоне),
# поэтому сразу после перетаскивания их не удаляем: старые сеансы убираются при следующем
# перетаскивании, а все — при выходе из программы.
DRAG_KEEP_SECONDS = 30 * 60


def drag_root() -> Path:
    """Временная папка программы для перетаскиваемых вложений: %TEMP%\\SimpleMail\\drag."""
    return Path(tempfile.gettempdir()) / "SimpleMail" / "drag"


def prepare_drag_files(files: list[tuple[str, bytes]]) -> list[Path]:
    """Записывает вложения в новый подкаталог drag_root() и возвращает пути к файлам."""
    root = drag_root()
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="drag-", dir=root))
    paths: list[Path] = []
    try:
        for name, data in files:
            target = unique_path(folder, safe_filename(name))
            with target.open("xb") as stream:
                stream.write(data)
            mark_from_internet(target)
            paths.append(target)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return paths


def cleanup_drag_files(max_age: float = DRAG_KEEP_SECONDS, now: float | None = None) -> None:
    """Удаляет сеансы перетаскивания старше max_age секунд (0 — все). Занятые файлы
    (Проводник ещё копирует) пропускаются — их уберёт следующая очистка."""
    root = drag_root()
    if not root.is_dir():
        return
    now = time.time() if now is None else now
    for folder in root.iterdir():
        try:
            if folder.is_dir() and not folder.is_symlink() and now - folder.stat().st_mtime >= max_age:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    for directory in (root, root.parent):  # пустые drag и SimpleMail не оставляем
        try:
            directory.rmdir()
        except OSError:
            break


class AttachmentList(QListWidget):
    """Список плашек вложений, из которого их можно перетащить наружу как файлы."""

    def __init__(self, bar: "AttachmentsBar"):
        super().__init__()
        self._bar = bar

    def startDrag(self, supported_actions):
        self._bar.start_drag()


class AttachmentsBar(QWidget):
    saved = Signal(str)  # сообщение для строки статуса

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files: list[tuple[str, bytes]] = []
        self.default_dir = ""  # папка «Вложения» ящика

        self.list = AttachmentList(self)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setStyleSheet("QListWidget{border:1px solid palette(mid);background:palette(base);}"
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
        cleanup_drag_files()  # остатки прошлого запуска (если программа была закрыта аварийно)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(lambda: cleanup_drag_files(0))

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
        self.list.setMovement(QListView.Static)  # Qt при этом выключает перетаскивание — включаем снова
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragOnly)
        self.list.setDefaultDropAction(Qt.CopyAction)
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
            it.setToolTip(tr("{0}\n{1}\nДвойной клик — открыть, перетащить — скопировать в папку, правый клик — меню", name, human_size(len(data))))
            it.setSizeHint(QSize(116, 62) if self.mode == "icons" else self._item_size(text))
            self.list.addItem(it)
        self._fit_height()

    # ---------- действия ----------
    def _selected_rows(self) -> list[int]:
        return sorted(self.list.row(it) for it in self.list.selectedItems())

    def start_drag(self) -> Qt.DropAction:
        """Перетаскивание выбранных плашек: файлы пишутся во временную папку программы,
        а Проводник/рабочий стол/другая программа копирует их к себе. Только копирование:
        при «перемещении» Проводник удалил бы исходник, а он нам ещё нужен для следующих раз."""
        rows = self._selected_rows()
        if not rows:
            return Qt.IgnoreAction
        cleanup_drag_files()
        try:
            paths = prepare_drag_files([self._files[r] for r in rows])
        except OSError as error:
            self.saved.emit(tr("Не удалось подготовить вложения для перетаскивания: {0}", str(error)))
            return Qt.IgnoreAction
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        drag = QDrag(self.list)
        drag.setMimeData(mime)
        pixmap = self.list.item(rows[0]).icon().pixmap(32, 32)
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
        result = drag.exec(Qt.CopyAction, Qt.CopyAction)
        if result == Qt.CopyAction:
            self.saved.emit(tr("Скопировано вложений: {0}", len(rows)))
        return result

    def _open(self, row: int):
        name, data = self._files[row]
        if is_dangerous(name):
            if QMessageBox.warning(self, tr("Осторожно"), tr("«{0}» может содержать исполняемый код или активное содержимое.\nОткрыть всё равно?", name), QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        path = temporary_attachment(name, data)
        mark_from_internet(path)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _save_one(self, row: int):
        name, data = self._files[row]
        start = str(Path(self.default_dir) / Path(name).name) if self.default_dir else Path(name).name
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить вложение"), start)
        if path:
            Path(path).write_bytes(data)
            mark_from_internet(path)
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
            target = unique_path(Path(folder), name)
            while True:
                try:
                    with target.open("xb") as stream:
                        stream.write(data)
                    break
                except FileExistsError:
                    target = unique_path(Path(folder), name)
            mark_from_internet(target)
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
