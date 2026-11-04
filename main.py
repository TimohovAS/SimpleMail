"""Запуск: python main.py  [--data ПАПКА_С_ДАННЫМИ]"""
import argparse
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from mailclient.db import Database, default_data_dir
from mailclient.ui.main_window import MainWindow


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=default_data_dir(), help="папка для базы писем")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("SimpleMail")
    db = Database(args.data / "mail.db")
    w = MainWindow(db)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
