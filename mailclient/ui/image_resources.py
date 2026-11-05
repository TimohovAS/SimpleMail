"""Decode inline raster images without SVG or external resource loading."""
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

FORMATS = {"image/png": b"PNG", "image/jpeg": b"JPEG", "image/gif": b"GIF", "image/bmp": b"BMP",
           "image/webp": b"WEBP", "image/tiff": b"TIFF", "image/x-icon": b"ICO"}


def raster_image(mime, data):
    format_name = FORMATS.get(mime.lower())
    if format_name:
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
        reader.setFormat(format_name.lower())
        reader.setAutoDetectImageFormat(False)
        image = reader.read()
        buffer.close()
        if not image.isNull():
            return image
    return None
