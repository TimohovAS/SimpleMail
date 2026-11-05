"""Запуск: python main.py  [--data ПАПКА_С_ДАННЫМИ]"""
import argparse
import sys
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mailclient import __version__
from mailclient.db import Database, default_data_dir
from mailclient.i18n import install_qt_translations, set_language
from mailclient.windows_integration import configure_taskbar_identity

_instance_lease = None


def main():
    global _instance_lease
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=default_data_dir(), help="папка для базы писем")
    ap.add_argument("--install-restore", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    from mailclient.logs import setup_logging
    try:
        setup_logging(args.data)
    except OSError:
        pass
    restore_error = None
    if args.install_restore:
        try:
            if args.wait_pid:
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    handle = kernel.OpenProcess(0x100000, False, args.wait_pid)
                    if handle:
                        try:
                            if kernel.WaitForSingleObject(handle, 120000) != 0:
                                raise RuntimeError("Previous SimpleMail process has not exited")
                        finally:
                            kernel.CloseHandle(handle)
                else:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        try:
                            os.kill(args.wait_pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.2)
                    else:
                        raise RuntimeError("Previous SimpleMail process has not exited")
            from mailclient.instance import acquire_instance
            _instance_lease = acquire_instance(args.data)
            if _instance_lease is None:
                return
            from mailclient.backup import install_restore
            install_restore(args.install_restore, args.data)
        except Exception as error:
            restore_error = str(error)
    from mailclient.instance import acquire_instance
    if _instance_lease is None:
        _instance_lease = acquire_instance(args.data)
    if _instance_lease is None:
        return
    db = Database(args.data / "mail.db")
    set_language(db.get_setting("language", "ru"))
    from mailclient.ui.main_window import MainWindow
    from mailclient.ui.viewer import register_schemes
    from mailclient.ui.icons import app_icon
    register_schemes()  # для просмотра HTML-писем; до создания QApplication
    configure_taskbar_identity()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("SimpleMail")
    app.setApplicationVersion(__version__)
    from mailclient.ui.theme import install_system_theme
    install_system_theme(app)
    app.setWindowIcon(app_icon())
    install_qt_translations(app)
    w = MainWindow(db)
    w.show()
    if restore_error:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(w, "SimpleMail", restore_error)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
