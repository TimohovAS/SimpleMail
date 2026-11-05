"""Значки панели инструментов (набор Lucide, лицензия ISC — icons/LICENSE-lucide.txt)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRect, QRectF, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from .theme import is_dark

ICON_DIR = Path(__file__).resolve().parent.parent / "icons"


def app_icon() -> QIcon:
    """Собственный значок приложения для окна, панели задач и ярлыков."""
    return QIcon(str(ICON_DIR / "simplemail.ico"))

# Цвета значков по смыслу действия
COLORS = {
    "mail-plus": "#2563eb", "reply": "#2563eb", "reply-all": "#2563eb", "forward": "#2563eb",
    "refresh-cw": "#16a34a", "mail-warning": "#dc2626", "shield-alert": "#dc2626",
    "mail-check": "#16a34a", "trash-2": "#6b7280", "send": "#2563eb", "inbox": "#2563eb",
}


DARK_COLORS = {
    "#2563eb": "#7db7ff", "#16a34a": "#6bce91", "#dc2626": "#ff8585",
    "#6b7280": "#a6aebb", "#374151": "#e5e7eb", "#9333ea": "#c4a5fa",
    "#ca8a04": "#f4c76b", "#0891b2": "#67d4e8", "#db2777": "#f68bbc",
}


class _ThemeIconEngine(QIconEngine):
    """Рисует уже выданные значки заново в цветах текущей палитры."""
    def __init__(self, svg: str, color: str | None):
        super().__init__()
        self.svg, self.color = svg, color

    def clone(self):
        return _ThemeIconEngine(self.svg, self.color)

    def isNull(self):
        return False

    def paint(self, painter, rect, mode, state):
        app = QApplication.instance()
        palette = app.palette() if app else QPalette()
        color = self.color or palette.color(QPalette.WindowText).name()
        if is_dark():
            color = DARK_COLORS.get(color, color)
        if mode == QIcon.Disabled:
            color = palette.color(QPalette.Disabled, QPalette.WindowText).name()
        elif mode == QIcon.Selected:
            color = palette.color(QPalette.HighlightedText).name()
        svg = self.svg.replace("currentColor", color)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        painter.save()
        painter.translate(rect.topLeft())
        renderer.render(painter, QRectF(0, 0, rect.width(), rect.height()))
        painter.restore()

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        pm = QPixmap(size * scale)
        pm.setDevicePixelRatio(scale)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pm


@lru_cache(maxsize=None)
def icon(name: str, color: str | None = None) -> QIcon:
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        return QIcon()
    svg = path.read_text(encoding="utf-8")
    return QIcon(_ThemeIconEngine(svg, color or COLORS.get(name)))
