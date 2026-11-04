"""Запуск долгих операций (сеть) в фоне, чтобы окно не зависало."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)
    progress = Signal(str)


class Task(QRunnable):
    def __init__(self, fn, *args, with_progress: bool = False, **kwargs):
        super().__init__()
        self.setAutoDelete(False)
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()
        if with_progress:
            self.kwargs["progress"] = self.signals.progress.emit

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            traceback.print_exc()
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        else:
            self.signals.done.emit(result)


_active: set[Task] = set()  # держим ссылки, пока задача работает


def run_task(fn, *args, on_done=None, on_error=None, on_progress=None, **kwargs) -> Task:
    t = Task(fn, *args, with_progress=on_progress is not None, **kwargs)
    _active.add(t)
    t.signals.done.connect(lambda _: _active.discard(t))
    t.signals.error.connect(lambda _: _active.discard(t))
    if on_done:
        t.signals.done.connect(on_done)
    if on_error:
        t.signals.error.connect(on_error)
    if on_progress:
        t.signals.progress.connect(on_progress)
    QThreadPool.globalInstance().start(t)
    return t
