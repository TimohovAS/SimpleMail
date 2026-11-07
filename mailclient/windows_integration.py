"""Значок на панели задач и автозапуск для текущего пользователя Windows."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

APP_ID = "TimohovAS.SimpleMail"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SimpleMail"
# При входе в Windows окно не открывается: программа сразу уходит в область уведомлений.
MINIMIZED_FLAG = "--minimized"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def startup_supported() -> bool:
    return os.name == "nt" and winreg is not None


def configure_taskbar_identity() -> None:
    if os.name != "nt":
        return
    try:
        function = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_long
        function(APP_ID)
    except (AttributeError, OSError):
        pass  # значок окна работает и без отдельной группы на панели задач


def startup_command(data_dir: Path, minimized: bool = True) -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        args = [str(executable)]
    else:
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
        args = [str(executable), str(PROJECT_DIR / "main.py")]
    args.extend(["--data", str(Path(data_dir).resolve())])
    if minimized:
        args.append(MINIMIZED_FLAG)
    return subprocess.list2cmdline(args)


def _startup_value() -> str:
    if not startup_supported():
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return ""
    return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) else ""


def startup_enabled() -> bool:
    return bool(_startup_value())


def refresh_startup_command(data_dir: Path) -> bool:
    """Запись автозапуска прежних версий (без MINIMIZED_FLAG) дополняет флагом.

    Меняется только запись, совпадающая с командой этой же копии программы: чужую или
    сделанную вручную запись не трогаем. Возвращает True, если запись обновлена."""
    current = _startup_value()
    if not current or current != startup_command(data_dir, minimized=False):
        return False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command(data_dir))
    return True


def set_startup_enabled(enabled: bool, data_dir: Path) -> None:
    if not startup_supported():
        raise OSError("Windows autostart is unavailable")
    if enabled:
        command = startup_command(data_dir)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            pass
