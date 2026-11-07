"""Значок на панели задач и автозапуск для текущего пользователя Windows."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

try:
    import winreg
except ImportError:
    winreg = None

APP_ID = "TimohovAS.SimpleMail"
# Автозапуск — ярлык в папке «Автозагрузка» пользователя. Запись в реестр (HKCU\...\Run)
# из самой программы антивирусы (Kaspersky PDM) принимают за закрепление трояна в системе.
SHORTCUT_NAME = "SimpleMail.lnk"
# Прежние версии (до 0.19.5) писали автозапуск сюда: запись только читаем и удаляем.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SimpleMail"
# При входе в Windows окно не открывается: программа сразу уходит в область уведомлений.
MINIMIZED_FLAG = "--minimized"
PROJECT_DIR = Path(__file__).resolve().parent.parent
CSIDL_STARTUP = 7


def startup_supported() -> bool:
    return os.name == "nt"


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


def startup_launch(data_dir: Path, minimized: bool = True) -> tuple[str, str]:
    """Программа и аргументы ярлыка автозапуска."""
    executable = Path(sys.executable).resolve()
    args = []
    if not getattr(sys, "frozen", False):
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
        args.append(str(PROJECT_DIR / "main.py"))
    args.extend(["--profile-fallback", str((Path(data_dir) / "mail.db").resolve().parent)])
    if minimized:
        args.append(MINIMIZED_FLAG)
    return str(executable), subprocess.list2cmdline(args)


def startup_shortcut() -> Path:
    """Ярлык в папке «Автозагрузка» текущего пользователя."""
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError(f"Startup folder is unavailable (0x{result & 0xFFFFFFFF:08X})")
    return Path(buffer.value) / SHORTCUT_NAME


def _legacy_run_value() -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return ""
    return value if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) else ""


def _remove_legacy_run_value() -> None:
    """Удаляет запись автозапуска прежних версий, только если она есть."""
    if not _legacy_run_value():
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def startup_enabled() -> bool:
    if not startup_supported():
        return False
    try:
        if startup_shortcut().is_file():
            return True
    except OSError:
        pass
    return bool(_legacy_run_value())


def set_startup_enabled(enabled: bool, data_dir: Path) -> None:
    if not startup_supported():
        raise OSError("Windows autostart is unavailable")
    shortcut = startup_shortcut()
    if enabled:
        target, arguments = startup_launch(data_dir)
        _write_shortcut(shortcut, target, arguments)
    else:
        try:
            shortcut.unlink()
        except FileNotFoundError:
            pass
    _remove_legacy_run_value()


def refresh_startup_profile(data_dir: Path) -> bool:
    """Ярлык автозапуска этой программы ведёт на текущую EXE и активный профиль.

    Меняется только существующий ярлык на SimpleMail.exe и только если он отличается.
    Запуск из исходников, выключенный автозапуск и записи в реестре не трогаем.
    Ошибки чтения/записи ярлыка передаются вызывающему."""
    if not getattr(sys, "frozen", False) or not startup_supported():
        return False
    shortcut = startup_shortcut()
    if not shortcut.is_file():
        return False
    target, arguments = _read_shortcut(shortcut)
    if PureWindowsPath(target).name.casefold() != "simplemail.exe":
        return False
    wanted_target, wanted_arguments = startup_launch(data_dir)
    if os.path.normcase(target) == os.path.normcase(wanted_target) and arguments == wanted_arguments:
        return False
    _write_shortcut(shortcut, wanted_target, wanted_arguments)
    return True


# --- Ярлыки Windows через COM (IShellLinkW), без pywin32 ---------------------------
CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
CLSCTX_INPROC_SERVER = 1
COINIT_APARTMENTTHREADED = 2
SLGP_RAWPATH = 4
STGM_READ = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _call(obj, index, argtypes, *args, restype=None):
    """Метод COM-объекта по номеру в таблице виртуальных функций."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype or ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return prototype(vtable[index])(obj, *args)


def _release(obj) -> None:
    if obj:
        _call(obj, 2, [], restype=ctypes.c_ulong)


@contextmanager
def _shell_link():
    ole32 = ctypes.windll.ole32
    initialized = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) in (0, 1)  # S_OK, S_FALSE
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_SHELL_LINK)), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(IID_ISHELL_LINK_W)), ctypes.byref(link))
        _call(link, 0, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],  # QueryInterface
              ctypes.byref(_guid(IID_IPERSIST_FILE)), ctypes.byref(persist))
        yield link, persist
    finally:
        _release(persist)
        _release(link)
        if initialized:
            ole32.CoUninitialize()


def _write_shortcut(path: Path, target: str, arguments: str) -> None:
    text = ctypes.c_wchar_p
    path.parent.mkdir(parents=True, exist_ok=True)
    with _shell_link() as (link, persist):
        _call(link, 20, [text], target)                                    # SetPath
        _call(link, 11, [text], arguments)                                 # SetArguments
        _call(link, 9, [text], str(Path(target).parent))                   # SetWorkingDirectory
        _call(link, 7, [text], "SimpleMail")                               # SetDescription
        _call(link, 17, [text, ctypes.c_int], target, 0)                   # SetIconLocation
        _call(persist, 6, [text, ctypes.c_int], str(path), 1)              # IPersistFile::Save


def _read_shortcut(path: Path) -> tuple[str, str]:
    text = ctypes.c_wchar_p
    target = ctypes.create_unicode_buffer(32768)
    arguments = ctypes.create_unicode_buffer(32768)
    with _shell_link() as (link, persist):
        _call(persist, 5, [text, ctypes.c_uint32], str(path), STGM_READ)   # IPersistFile::Load
        _call(link, 3, [text, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],  # GetPath
              target, len(target), None, SLGP_RAWPATH)
        _call(link, 10, [text, ctypes.c_int], arguments, len(arguments))   # GetArguments
    return target.value, arguments.value
