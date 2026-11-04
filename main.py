"""Запуск: python main.py  [--data ПАПКА_С_ДАННЫМИ]"""
import argparse
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mailclient.db import Database, default_data_dir
from mailclient.i18n import install_qt_translations, set_language


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=default_data_dir(), help="папка для базы писем")
    args = ap.parse_args()
    db = Database(args.data / "mail.db")
    set_language(db.get_setting("language", "ru"))
    from mailclient.ui.main_window import MainWindow
    from mailclient.ui.viewer import register_schemes
    register_schemes()  # для просмотра HTML-писем; до создания QApplication
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("SimpleMail")
    install_qt_translations(app)
    w = MainWindow(db)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
