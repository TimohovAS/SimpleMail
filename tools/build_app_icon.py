"""Пересобрать PNG и Windows ICO из SVG: python tools/build_app_icon.py."""
from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parents[1] / "mailclient" / "icons"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(renderer, size):
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Could not encode icon")
    return bytes(buffer.data())


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(ICON_DIR / "simplemail.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid icon SVG")
    images = [render(renderer, size) for size in SIZES]
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in zip(SIZES, images):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (ICON_DIR / "simplemail.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(images))
    (ICON_DIR / "simplemail.png").write_bytes(render(renderer, 512))
    print("Created simplemail.ico (7 sizes) and simplemail.png")


if __name__ == "__main__":
    main()
