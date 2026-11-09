"""Запуск: python main.py  [--data ПАПКА_С_ДАННЫМИ] [--minimized]"""
import sys

if __name__ == "__main__":
    # Заставка — до загрузки Qt: иначе несколько секунд после запуска на экране ничего нет.
    from mailclient import splash
    splash.show(sys.argv)

import argparse
import os
import sqlite3
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mailclient import __version__
from mailclient.db import Database, SchemaMismatch, default_data_dir, remove_database, set_aside_database
from mailclient.i18n import early_language, install_qt_translations, startup_language, tr
from mailclient.windows_integration import configure_taskbar_identity
from mailclient.profiles import ProfileError, remember_profile, select_data_dir

_instance_lease = None
_installation_lease = None


def create_application() -> QApplication:
    """Единственный QApplication программы: со схемами просмотра, темой, значком и переводом Qt."""
    from mailclient.ui.viewer import register_schemes
    from mailclient.ui.icons import app_icon
    from mailclient.ui.theme import install_system_theme
    register_schemes()  # для просмотра HTML-писем; до создания QApplication
    configure_taskbar_identity()
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("SimpleMail")
    app.setApplicationVersion(__version__)
    install_system_theme(app)
    app.setWindowIcon(app_icon())
    install_qt_translations(app)
    return app


def open_other_schema(error: SchemaMismatch) -> Database | None:
    """База другой версии. Пустую (без ящиков и писем) можно заменить новой — с согласия
    пользователя; базу с данными программа не трогает. None — выйти."""
    from PySide6.QtWidgets import QMessageBox
    if not error.empty:
        QMessageBox.critical(None, "SimpleMail", tr(
            "База писем создана другой версией SimpleMail (схема {0}, нужна {1}):\n{2}\n\n"
            "Программа её не изменяла и открыть не может. Обратитесь к администратору SimpleMail: "
            "базу нужно обновить до текущей версии.",
            error.found, error.expected, error.path))
        return None
    box = QMessageBox(QMessageBox.Question, "SimpleMail", tr(
        "База писем создана другой версией SimpleMail (схема {0}, нужна {1}):\n{2}\n\n"
        "В ней нет ни ящиков, ни писем. Удалить её и создать новую пустую базу?",
        error.found, error.expected, error.path))
    create = box.addButton(tr("Создать новую базу"), QMessageBox.AcceptRole)
    box.addButton(tr("Выход"), QMessageBox.RejectRole)
    box.setDefaultButton(create)
    from PySide6.QtWidgets import QCheckBox
    keep = QCheckBox(tr("Сохранить старый файл как копию"))
    box.setCheckBox(keep)
    box.exec()
    if box.clickedButton() is not create:
        return None
    import logging
    try:
        if keep.isChecked():
            logging.getLogger("simplemail").info(
                "Пустая база схемы %s сохранена как %s", error.found, set_aside_database(error.path))
        else:
            remove_database(error.path)
            logging.getLogger("simplemail").info("Пустая база схемы %s удалена", error.found)
        return Database(error.path)
    except (OSError, SchemaMismatch) as failure:
        QMessageBox.critical(None, "SimpleMail", tr("Не удалось создать новую базу писем: {0}", str(failure)))
        return None


def close_splash():
    from mailclient import splash
    splash.close()


def main():
    global _instance_lease, _installation_lease
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, help="папка для базы писем; запоминается для следующих запусков")
    ap.add_argument("--profile-fallback", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--isolated-profile", action="store_true", help="отдельный тестовый профиль --data без смены рабочего профиля")
    ap.add_argument("--minimized", action="store_true",
                    help="запуск без окна, только значок в области уведомлений (автозапуск Windows)")
    ap.add_argument("--install-restore", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    early_language((args.data or default_data_dir()) / "mail.db")
    try:
        args.data = select_data_dir(args.data, args.profile_fallback, args.isolated_profile)
    except ProfileError as error:
        app = create_application()
        close_splash()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, tr("Профиль почты недоступен"), str(error))
        return
    # Окна до открытия базы — уже на языке пользователя или установщика.
    early_language(args.data / "mail.db")
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
        activated = notify_running(args.data)  # заставка видна, пока ждём ответа
        close_splash()
        if not activated:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(None, "SimpleMail", tr(
                "SimpleMail уже запущен или ещё завершает работу.\nПодождите несколько секунд и запустите снова."))
        return
    app = None
    try:
        db = Database(args.data / "mail.db")
    except SchemaMismatch as error:
        app = create_application()
        close_splash()
        db = open_other_schema(error)
        if db is None:
            return
    except (OSError, sqlite3.Error) as error:
        app = create_application()
        close_splash()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, tr("Профиль почты недоступен"), tr(
            "Не удалось открыть базу писем:\n{0}\n\n{1}\n\n"
            "Данные сохранены на прежнем месте. Проверьте доступ к папке и повторите запуск.",
            args.data / "mail.db", error))
        return
    startup_language(db)
    from mailclient.passwords import initialize_keyring
    initialize_keyring(background=True)  # ~0,3 с: параллельно с созданием окна
    from mailclient.ui.main_window import MainWindow
    if app is None:
        app = create_application()
    else:
        install_qt_translations(app)  # язык мог смениться после открытия новой базы
    w = MainWindow(db)
    w.isolated_profile = args.isolated_profile

    def save_profile_location():
        if args.isolated_profile:
            return
        try:
            if remember_profile(db) and not os.environ.get("SIMPLEMAIL_PROFILE_FILE"):
                from mailclient.windows_integration import refresh_startup_profile
                refresh_startup_profile(Path(db.path).resolve().parent)
        except ProfileError as error:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(w, tr("Не удалось запомнить профиль почты"), str(error))
        except OSError:
            import logging
            logging.getLogger("simplemail").warning("Не удалось обновить путь автозапуска", exc_info=True)

    w.profile_changed.connect(save_profile_location)
    # Окно может быть спрятано в область уведомлений: программа завершается только по
    # настоящему закрытию главного окна (меню «Выход» или крестик без сворачивания в трей).
    app.setQuitOnLastWindowClosed(False)
    w.closed.connect(app.quit)
    app.commitDataRequest.connect(lambda _manager: setattr(w, "_quitting", True))  # выход из Windows
    if not (args.minimized and w.can_start_hidden() and not restore_error):
        w.show()
    close_splash()
    from mailclient.splash import seconds_since_start
    if (uptime := seconds_since_start()) is not None:
        import logging
        logging.getLogger("simplemail").info("Запуск: главное окно через %.1f с после старта процесса", uptime)
    save_profile_location()

    activation = start_activation_server(args.data, w.bring_to_front, app)
    if restore_error:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(w, "SimpleMail", restore_error)
    code = app.exec()
    activation.close()
    # Фоновые задачи (сеть) могут ждать ответа сервера до 30 с. Не держим процесс:
    # иначе повторный запуск сразу после закрытия «ничего не делает».
    from mailclient.shutdown import force_exit, wait_for_mail_operations, wait_for_workers
    # Удаления, начатые до выхода, доводятся до конца (письма уже убраны из списка).
    wait_for_mail_operations(getattr(w, "pending_mail_operations", []))
    workers_finished = wait_for_workers(3000)
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
        force_exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
