"""Перетаскивание файлов в окно письма: какие файлы перетаскивают и подсветка зоны."""
from __future__ import annotations

from ..i18n import tr

from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import QWidget

# Картинки, которые можно вставить прямо в текст (их умеет показать редактор).
INLINE_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                      ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp"}

ZONE_ATTACH = "attach"
ZONE_INLINE = "inline"


def dropped_files(mime) -> list[Path]:
    """Файлы с диска из перетаскивания. Папки и ссылки из браузера не считаются."""
    if mime is None or not mime.hasUrls():
        return []
    files = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        if path.is_file():
            files.append(path)
    return files


def all_inline_images(files) -> bool:
    return bool(files) and all(Path(f).suffix.lower() in INLINE_IMAGE_TYPES for f in files)


def image_type(path) -> str:
    return INLINE_IMAGE_TYPES.get(Path(path).suffix.lower(), "image/png")


class DropOverlay(QWidget):
    """Полупрозрачная подсветка поверх окна, пока над ним держат файлы.

    Не мешает самому перетаскиванию: события мыши проходят сквозь неё к полям окна.
    Если перетаскивают только картинки, текст письма — отдельная зона «вставить в текст».
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.inline_rect: QRect | None = None
        self.zone = ZONE_ATTACH
        self.hide()

    def show_zones(self, inline_rect: QRect | None, zone: str):
        changed = (not self.isVisible() or self.zone != zone or self.inline_rect != inline_rect
                   or self.geometry() != self.parentWidget().rect())
        self.inline_rect, self.zone = inline_rect, zone
        if not changed:
            return
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self.update()

    def attach_rect(self) -> QRect:
        r = self.rect().adjusted(4, 4, -5, -5)
        if self.inline_rect is not None:
            r.setBottom(max(r.top() + 24, self.inline_rect.top() - 4))
        return r

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        zones = [(self.attach_rect(), ZONE_ATTACH, tr("Вложить файлы"))]
        if self.inline_rect is not None:
            zones.append((self.inline_rect.adjusted(0, 0, -1, -1), ZONE_INLINE, tr("Вставить картинки в текст")))
        pal = self.palette()
        accent = pal.color(QPalette.Highlight)
        back = QColor(pal.color(QPalette.Window)); back.setAlpha(215)
        font = QFont(self.font()); font.setBold(True); font.setPointSizeF(font.pointSizeF() * 1.3)
        p.setFont(font)
        for rect, zone, label in zones:
            active = zone == self.zone
            tint = QColor(accent); tint.setAlpha(70 if active else 20)
            p.setPen(Qt.NoPen)
            p.setBrush(back); p.drawRoundedRect(rect, 8, 8)
            p.setBrush(tint); p.drawRoundedRect(rect, 8, 8)
            pen = QPen(accent, 2.5 if active else 1.5, Qt.SolidLine if active else Qt.DashLine)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, 8, 8)
            p.setPen(pal.color(QPalette.WindowText) if active else pal.color(QPalette.PlaceholderText))
            p.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, label)
        p.end()
