"""Значок на панели задач и автозапуск для текущего пользователя Windows."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

try:
    import winreg
except ImportError:
    winreg = None

APP_ID = "TimohovAS.SimpleMail"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SimpleMail"
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


def startup_command(data_dir: Path) -> str:
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        args = [str(executable)]
    else:
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
        args = [str(executable), str(PROJECT_DIR / "main.py")]
    args.extend(["--profile-fallback", str((Path(data_dir) / "mail.db").resolve().parent)])
    return subprocess.list2cmdline(args)


def startup_enabled() -> bool:
    if not startup_supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
        return kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and bool(value)
    except OSError:
        return False


def refresh_startup_profile(data_dir: Path) -> bool:
    """Preserve enabled SimpleMail startup while replacing its legacy --data hint.

    Only a packaged application can adopt an existing SimpleMail executable
    entry. Source runs, disabled startup and unrelated commands remain intact.
    Registry failures propagate so the caller can report an incomplete update.
    """
    if not getattr(sys, "frozen", False) or not startup_supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except FileNotFoundError:
        return False
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            return False
        executable = text[1:end]
    else:
        executable = text.split(None, 1)[0]
    if PureWindowsPath(executable).name.casefold() != "simplemail.exe":
        return False
    command = startup_command(data_dir)
    if value == command and kind == winreg.REG_SZ:
        return False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
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
