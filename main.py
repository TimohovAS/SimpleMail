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
from mailclient.i18n import install_qt_translations, startup_language, tr
from mailclient.windows_integration import configure_taskbar_identity

_instance_lease = None
_installation_lease = None


def main():
    global _instance_lease, _installation_lease
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
    from mailclient.updates import hold_installation_mutex
    _installation_lease = hold_installation_mutex()
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
    from mailclient.instance import acquire_instance, notify_running, start_activation_server
    if _instance_lease is None:
        _instance_lease = acquire_instance(args.data)
    if _instance_lease is None:
        # Программа уже запущена: показываем её окно, а не молча выходим.
        app = QApplication(sys.argv)
        if not notify_running(args.data):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(None, "SimpleMail", tr(
                "SimpleMail уже запущен или ещё завершает работу.\nПодождите несколько секунд и запустите снова."))
        return
    db = Database(args.data / "mail.db")
    startup_language(db)
    from mailclient.passwords import initialize_keyring
    initialize_keyring()
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
    # Окно может быть спрятано в область уведомлений: программа завершается только по
    # настоящему закрытию главного окна (меню «Выход» или крестик без сворачивания в трей).
    app.setQuitOnLastWindowClosed(False)
    w.closed.connect(app.quit)
    app.commitDataRequest.connect(lambda _manager: setattr(w, "_quitting", True))  # выход из Windows
    w.show()

    activation = start_activation_server(args.data, w.bring_to_front, app)
    if restore_error:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(w, "SimpleMail", restore_error)
    code = app.exec()
    activation.close()
    # Фоновые задачи (сеть) могут ждать ответа сервера до 30 с. Не держим процесс:
    # иначе повторный запуск сразу после закрытия «ничего не делает».
    from PySide6.QtCore import QThreadPool
    workers_finished = QThreadPool.globalInstance().waitForDone(3000)
    pending_update = getattr(app, "_pending_update", None)
    if pending_update is not None:
        from PySide6.QtWidgets import QMessageBox
        from mailclient.updates import launch_installer
        db.close()
        try:
            launch_installer(*pending_update)
        except Exception as error:
            QMessageBox.critical(None, "SimpleMail", tr("Не удалось запустить установщик: {0}", str(error)))
            code = 1
    if not workers_finished:
        import logging
        logging.getLogger("simplemail").info("Выход без ожидания фоновых сетевых операций")
        logging.shutdown()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
