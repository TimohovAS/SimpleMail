"""Заставка запуска: маленькое окно Windows сразу после старта процесса.

Загрузка Qt и встроенного браузера (Qt WebEngine) у установленной программы занимает
секунды, особенно при первом запуске после установки или включения компьютера, когда
антивирус проверяет библиотеки. Чтобы не казалось, что программа не запустилась, окно
«SimpleMail запускается…» показывается ещё до импорта Qt — средствами Windows (ctypes),
в отдельном потоке. Закрывается, когда появилось главное окно или окно с вопросом.

Окно и его элементы рисует сама Windows (стандартные классы STATIC), без кода на Python:
заставка остаётся нарисованной, даже пока основной поток занят загрузкой библиотек.
Модуль не импортирует ничего тяжёлого и ничего не делает вне Windows.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_splash = None
_used = False  # заставка показывается один раз за запуск


def _text() -> tuple[str, str]:
    """Подпись на языке системы (база с выбором языка ещё не открыта)."""
    lang = ""
    try:
        import ctypes
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        lang = {0x19: "ru", 0x1A: "sr"}.get(lang_id, "")
    except (AttributeError, OSError):
        pass
    try:  # язык, выбранный в установщике, важнее языка Windows
        installed = Path(sys.executable).with_name("installer-language.txt").read_text(encoding="utf-8-sig").strip()
        lang = {"ru": "ru", "sr_Latn": "sr"}.get(installed, lang)
    except (OSError, UnicodeError):
        pass
    if lang == "sr":
        return "Pokretanje programa…", "Prvo pokretanje posle instalacije može da potraje."
    if lang == "ru":
        return "Запуск программы…", "Первый запуск после установки может занять немного больше времени."
    return "Starting…", "The first start after installation may take a little longer."


def _icon_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "mailclient" / "icons" / "simplemail.ico"


class _Splash:
    def __init__(self, version: str):
        self.version = version
        self.hwnd = None
        self.thread_id = None
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="splash", daemon=True)
        self._thread.start()
        self._ready.wait(1.0)  # окно создано (или не удалось) — дальше грузим Qt

    def _run(self):
        try:
            self._create()
        except Exception:
            self.hwnd = None
        finally:
            self._ready.set()
        if self.hwnd:
            self._loop()

    def _create(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = user32
        # Только этот поток — с учётом масштаба экрана (чёткий текст на 125–200 %);
        # режим всего процесса потом выбирает Qt, как и раньше.
        try:
            user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            dpi = user32.GetDpiForSystem()
        except AttributeError:  # Windows старше 10 (1607)
            dpi = 96
        scale = (dpi or 96) / 96

        def px(value):
            return int(round(value * scale))

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UpdateWindow.argtypes = [wintypes.HWND]
        user32.GetSysColorBrush.restype = wintypes.HBRUSH
        user32.GetSysColorBrush.argtypes = [ctypes.c_int]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        gdi32.CreateFontW.restype = wintypes.HFONT
        gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        instance = kernel32.GetModuleHandleW(None)
        # Оконная процедура — сама Windows (DefWindowProcW), а не функция на Python:
        # окну не нужен интерпретатор, чтобы отвечать системе и перерисовываться.
        default_proc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
        wc = WNDCLASSW()
        wc.lpfnWndProc = default_proc
        wc.hInstance = instance
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32650))  # IDC_APPSTARTING: «работаю»
        wc.hbrBackground = user32.GetSysColorBrush(15)  # COLOR_3DFACE — как у окон-сообщений
        wc.lpszClassName = "SimpleMailSplash"
        user32.RegisterClassW(ctypes.byref(wc))

        width, height = px(400), px(116)
        x = (user32.GetSystemMetrics(0) - width) // 2   # SM_CXSCREEN
        y = (user32.GetSystemMetrics(1) - height) // 2  # SM_CYSCREEN
        WS_POPUP, WS_BORDER, WS_CHILD, WS_VISIBLE = 0x80000000, 0x00800000, 0x40000000, 0x10000000
        WS_EX_APPWINDOW = 0x40000
        # Кнопка на панели задач: видно, что программа запускается, даже если заставку закрыли другие окна.
        hwnd = user32.CreateWindowExW(WS_EX_APPWINDOW, wc.lpszClassName, "SimpleMail", WS_POPUP | WS_BORDER,
                                      x, y, width, height, None, None, instance, None)
        if not hwnd:
            return
        icon = user32.LoadImageW(None, str(_icon_path()), 1, px(48), px(48), 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if icon:
            small = user32.LoadImageW(None, str(_icon_path()), 1, px(16), px(16), 0x10)
            user32.SendMessageW(hwnd, 0x80, 1, icon)           # WM_SETICON big
            user32.SendMessageW(hwnd, 0x80, 0, small or icon)  # WM_SETICON small

        def font(size, bold=False):
            return gdi32.CreateFontW(-px(size), 0, 0, 0, 700 if bold else 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")

        def label(text, left, top, w, h, fnt, style=0):
            control = user32.CreateWindowExW(0, "STATIC", text, WS_CHILD | WS_VISIBLE | style,
                                             left, top, w, h, hwnd, None, instance, None)
            if control and fnt:
                user32.SendMessageW(control, 0x30, fnt, 0)  # WM_SETFONT
            return control

        title, hint = _text()
        if icon:
            picture = user32.CreateWindowExW(0, "STATIC", None, WS_CHILD | WS_VISIBLE | 0x3,  # SS_ICON
                                             px(20), px(20), px(48), px(48), hwnd, None, instance, None)
            user32.SendMessageW(picture, 0x170, icon, 0)  # STM_SETICON
        text_left = px(84)
        label(f"SimpleMail {self.version}", text_left, px(16), width - text_left - px(16), px(26), font(15, True))
        label(title, text_left, px(44), width - text_left - px(16), px(22), font(10.5))
        label(hint, text_left, px(70), width - text_left - px(16), px(36), font(8.5))
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: фокус не забираем — его получит главное окно
        user32.UpdateWindow(hwnd)
        self.thread_id = kernel32.GetCurrentThreadId()
        self.hwnd = hwnd
        self._keep = wc

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        user32 = self.user32
        msg = wintypes.MSG()
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.thread_id:
            import ctypes
            # WM_QUIT: поток выходит из цикла сообщений и сам уничтожает своё окно.
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, 0x12, 0, 0)
            self._thread.join(1.0)


def show(argv=None) -> None:
    """Показать заставку (только Windows, не при запуске в трей и не во время тестов)."""
    global _splash, _used
    argv = sys.argv if argv is None else argv
    if _used or os.name != "nt" or os.environ.get("SIMPLEMAIL_NO_SPLASH"):
        return
    if "--minimized" in argv or "--install-restore" in argv:
        return
    _used = True
    from . import __version__
    try:
        _splash = _Splash(__version__)
    except Exception:
        _splash = None
_used = False  # заставка показывается один раз за запуск


def close() -> None:
    """Убрать заставку. Можно вызывать сколько угодно раз и без show()."""
    global _splash
    splash, _splash = _splash, None
    if splash is not None:
        try:
            splash.close()
        except Exception:
            pass


def seconds_since_start() -> float | None:
    """Сколько секунд прошло с запуска процесса (для журнала: как быстро появилось окно)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32")
        created, exited, kernel, user, now = (wintypes.FILETIME() for _ in range(5))
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE  # псевдодескриптор -1
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
        if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))

        def ticks(value):
            return (value.dwHighDateTime << 32) | value.dwLowDateTime
        return (ticks(now) - ticks(created)) / 10_000_000
    except Exception:  # только для журнала: запуск из-за этого не должен прерываться
        return None
