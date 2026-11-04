"""Хранение писем файлами .eml в папке ящика.

Структура папки ящика:
    <папка>/Входящие/2026-11/2026-11-04_101500_uid1234.eml
    <папка>/Отправленные/2026-11/...
    <папка>/Вложения/        — сюда по умолчанию сохраняются вложения
В базе (mail.db) хранится только список писем для быстрого поиска и путь к файлу.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

FOLDER_NAMES = {"inbox": "Входящие", "sent": "Отправленные", "trash": "Входящие"}
ATTACHMENTS_DIR = "Вложения"


def documents_dir() -> Path:
    if os.name == "nt":
        try:  # настоящая папка «Документы» (может быть перенесена, например в OneDrive)
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # CSIDL_PERSONAL
            if buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Documents"


def safe_name(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .") or "mailbox"


def default_storage_dir(email: str) -> str:
    return str(documents_dir() / "SimpleMail" / safe_name(email))


def eml_path(storage_dir: str, folder: str, date_ts: int, uid: int | None, raw: bytes) -> Path:
    dt = datetime.fromtimestamp(date_ts) if date_ts else datetime.now()
    tag = f"uid{uid}" if uid is not None else hashlib.sha1(raw).hexdigest()[:10]
    return (Path(storage_dir) / FOLDER_NAMES.get(folder, folder) / dt.strftime("%Y-%m")
            / f"{dt:%Y-%m-%d_%H%M%S}_{tag}.eml")


def write_eml(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)


def read_eml(path: str | None) -> bytes:
    if not path:
        return b""
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def attachments_dir(storage_dir: str) -> Path:
    p = Path(storage_dir) / ATTACHMENTS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def move_file(src: str, old_root: str, new_root: str) -> str:
    """Перенести файл письма из старой папки ящика в новую, сохранив подпапки."""
    s = Path(src)
    try:
        rel = s.relative_to(old_root)
    except ValueError:
        return src  # файл лежит не в старой папке — не трогаем
    dst = Path(new_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if s.exists():
        shutil.move(str(s), str(dst))
    return str(dst)
