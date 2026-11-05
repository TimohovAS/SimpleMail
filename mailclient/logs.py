"""Журнал ошибок: %APPDATA%\\SimpleMail\\logs\\simplemail.log (при запуске через pythonw
вывод в консоль теряется, поэтому всё важное пишем в файл)."""
from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("simplemail")
_fault_file = None


def log_dir(data_dir) -> Path:
    return Path(data_dir) / "logs"


def setup_logging(data_dir) -> Path:
    """Включить запись журнала; вернуть путь к файлу."""
    global _fault_file
    folder = log_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "simplemail.log"
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    def excepthook(kind, value, tb):
        log.critical("Необработанная ошибка", exc_info=(kind, value, tb))
        if sys.__stderr__ is not None:
            sys.__excepthook__(kind, value, tb)

    def thread_excepthook(args):
        log.critical("Необработанная ошибка в потоке %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    try:  # аварийное завершение Python/Qt — тоже в журнал
        _fault_file = open(folder / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(_fault_file)
    except OSError:
        pass
    return path
