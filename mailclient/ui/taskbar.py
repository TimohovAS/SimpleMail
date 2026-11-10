"""Кнопка SimpleMail в панели задач Windows: жёлтый конвертик с числом непрочитанных поверх значка.

Используется ITaskbarList3::SetOverlayIcon. Наложение действует, только пока у окна есть кнопка
в панели задач: Windows присылает окну сообщение «TaskbarButtonCreated» каждый раз, когда кнопка
появляется (первый показ, показ после сворачивания к часам, перезапуск Проводника), — тогда значок
ставится заново. На других системах ничего не делает.
"""
from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

from ..i18n import tr
from .tray import badge_text, paint_envelope

log = logging.getLogger(__name__)

CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
IID_ITASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
SM_CXSMICON = 49
OVERLAY_SIZE = 16


def overlay_image(count: int, size: int = OVERLAY_SIZE) -> QImage:
    """Конвертик на весь квадрат (Windows рисует его в правом нижнем углу кнопки); пустой при 0."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    text = badge_text(count)
    if text:
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        edge = max(1.0, size / 16)
        top = size * 0.14
        body = QRectF(edge / 2, top + edge / 2, size - edge, size - top - edge)
        paint_envelope(painter, body, text, edge)
        painter.end()
    return image


if sys.platform == "win32":
    from ctypes import wintypes

    from ..windows_integration import CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, _GUID, _call, _guid

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.CreateIconIndirect.argtypes = [ctypes.POINTER(_ICONINFO)]
    _user32.CreateIconIndirect.restype = wintypes.HICON
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BITMAPINFOHEADER), wintypes.UINT,
                                        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    _gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    _gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    _gdi32.CreateBitmap.restype = wintypes.HBITMAP
    _gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    def _small_icon_size(hwnd: int) -> int:
        """Размер маленького значка для DPI монитора окна (16 при 100 %, 24 при 150 % …)."""
        try:
            dpi = _user32.GetDpiForWindow(wintypes.HWND(hwnd))
            size = _user32.GetSystemMetricsForDpi(SM_CXSMICON, dpi) if dpi else 0
        except (AttributeError, OSError):  # Windows старше 10 1607
            size = 0
        return size or _user32.GetSystemMetrics(SM_CXSMICON) or OVERLAY_SIZE

    def hicon_from_image(image: QImage) -> int:
        """HICON с альфа-каналом из QImage; освобождается DestroyIcon."""
        image = image.convertToFormat(QImage.Format_ARGB32)
        width, height = image.width(), image.height()
        header = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=width,
                                   biHeight=-height, biPlanes=1, biBitCount=32)  # строки сверху вниз
        bits = ctypes.c_void_p()
        screen = _user32.GetDC(None)
        color = _gdi32.CreateDIBSection(screen, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        _user32.ReleaseDC(None, screen)
        if not color or not bits:
            raise ctypes.WinError(ctypes.get_last_error())
        mask = None
        try:
            pixels = bytes(image.constBits())[:width * height * 4]  # ARGB32 в памяти = BGRA, как у DIB
            ctypes.memmove(bits, pixels, len(pixels))
            mask_bits = ctypes.create_string_buffer(((width + 15) // 16) * 2 * height)  # нули: берётся альфа
            mask = _gdi32.CreateBitmap(width, height, 1, 1, mask_bits)
            info = _ICONINFO(fIcon=True, hbmMask=mask, hbmColor=color)
            icon = _user32.CreateIconIndirect(ctypes.byref(info))
            if not icon:
                raise ctypes.WinError(ctypes.get_last_error())
            return icon
        finally:
            _gdi32.DeleteObject(color)
            if mask:
                _gdi32.DeleteObject(mask)

    def _taskbar_list():
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)  # обычно уже сделано Qt
        taskbar = ctypes.c_void_p()
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_TASKBAR_LIST)), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(IID_ITASKBAR_LIST3)), ctypes.byref(taskbar))
        _call(taskbar, 3, [])                                             # HrInit
        return taskbar

    def _set_overlay(taskbar, hwnd: int, icon, description) -> None:
        _call(taskbar, 18, [wintypes.HWND, wintypes.HICON, wintypes.LPCWSTR],  # SetOverlayIcon
              hwnd, icon, description)


class TaskbarBadge(QAbstractNativeEventFilter):
    """Число непрочитанных на кнопке окна в панели задач Windows."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.count = 0
        self._applied = None          # (hwnd, count, size) — что уже показано
        self._taskbar = None
        self._warned = False
        self.available = sys.platform == "win32"
        if not self.available:
            return
        self._button_created = _user32.RegisterWindowMessageW("TaskbarButtonCreated")
        app = QGuiApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)

    def set_unread(self, count: int) -> None:
        self.count = max(0, count)
        self.apply()

    def refresh(self) -> None:
        """Кнопку пересоздали (окно снова показано, перезапустили Проводник): поставить значок заново."""
        self._applied = None
        self.apply()

    def close(self) -> None:
        """Окно закрывается насовсем: больше не следим за сообщениями."""
        if self.available:
            self.available = False
            app = QGuiApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        try:
            # На Windows message — всегда указатель на MSG (windows_generic_MSG / windows_dispatcher_MSG).
            if self._button_created and wintypes.MSG.from_address(int(message)).message == self._button_created:
                self.refresh()
        except Exception:  # фильтр не должен ломать обработку сообщений окна
            log.debug("TaskbarButtonCreated", exc_info=True)
        return False, 0

    def apply(self) -> None:
        if not self.available:
            return
        # Не создаём окно раньше времени: winId() у ещё не показанного окна создал бы его.
        if self.window.windowHandle() is None or not self.window.isVisible():
            return
        hwnd = int(self.window.winId())
        size = _small_icon_size(hwnd)
        key = (hwnd, self.count, size)
        if key == self._applied:
            return
        icon = None
        try:
            if self._taskbar is None:
                self._taskbar = _taskbar_list()
            if self.count:
                icon = hicon_from_image(overlay_image(self.count, size))
                _set_overlay(self._taskbar, hwnd, icon, tr("SimpleMail — непрочитанных: {0}", self.count))
            else:
                _set_overlay(self._taskbar, hwnd, None, None)
            self._applied = key
        except OSError:
            # Без панели задач (нет Проводника) — не показываем; попробуем снова при следующем изменении
            # или когда появится кнопка. В журнал — один раз.
            self._taskbar = None
            if not self._warned:
                self._warned = True
                log.warning("Не удалось показать число писем на кнопке в панели задач", exc_info=True)
        finally:
            if icon:
                _user32.DestroyIcon(icon)  # панель задач хранит свою копию
