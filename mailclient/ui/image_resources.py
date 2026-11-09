"""Decode inline raster images and store them in self-contained signature HTML."""
import base64
import re
import uuid

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

FORMATS = {"image/png": b"PNG", "image/jpeg": b"JPEG", "image/gif": b"GIF", "image/bmp": b"BMP",
           "image/webp": b"WEBP", "image/tiff": b"TIFF", "image/x-icon": b"ICO"}
IMAGE_SOURCE = re.compile(r"(<img\b[^>]*?\s+src\s*=\s*)([\"'])(.*?)\2", re.I)


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


def embed_inline_images(html, images):
    """Persist editor images in signature HTML without referencing a file or CID."""
    sources = {f"cid:{cid}": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
               for cid, mime, data in images if mime.lower() in FORMATS}

    def replace(match):
        source = sources.get(match[3])
        return f"{match[1]}{match[2]}{source}{match[2]}" if source else match[0]

    return IMAGE_SOURCE.sub(replace, html)


def extract_inline_images(html):
    """Turn embedded signature images into CID resources for an editor or message."""
    images = []

    def replace(match):
        header, _, encoded = match[3].partition(",")
        mime = header.removeprefix("data:").removesuffix(";base64").lower()
        if not header.startswith("data:") or not header.endswith(";base64") or mime not in FORMATS:
            return match[0]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            return match[0]
        if raster_image(mime, data) is None:
            return match[0]
        cid = f"{uuid.uuid4().hex}@simplemail"
        images.append((cid, mime, data))
        return f"{match[1]}{match[2]}cid:{cid}{match[2]}"

    return IMAGE_SOURCE.sub(replace, html), images
