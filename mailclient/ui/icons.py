"""Значки панели инструментов (набор Lucide, лицензия ISC — icons/LICENSE-lucide.txt)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ICON_DIR = Path(__file__).resolve().parent.parent / "icons"

# Цвета значков по смыслу действия
COLORS = {
    "mail-plus": "#2563eb", "reply": "#2563eb", "reply-all": "#2563eb", "forward": "#2563eb",
    "refresh-cw": "#16a34a", "mail-warning": "#dc2626", "shield-alert": "#dc2626",
    "mail-check": "#16a34a", "trash-2": "#6b7280", "send": "#2563eb", "inbox": "#2563eb",
}


def _text_color() -> str:
    app = QApplication.instance()
    return app.palette().windowText().color().name() if app else "#333333"


@lru_cache(maxsize=None)
def icon(name: str, color: str | None = None) -> QIcon:
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        return QIcon()
    svg = path.read_text(encoding="utf-8").replace("currentColor", color or COLORS.get(name) or _text_color())
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    result = QIcon()
    for size in (16, 20, 24, 32, 48):
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        result.addPixmap(pm)
    return result
