"""Bounded worker shutdown and safe forced termination of this process only."""
from __future__ import annotations

import os
import time


def wait_for_workers(timeout_ms: int = 3000) -> bool:
    """Drain local and network pools using one shared deadline.

    Local tasks can persist read flags or drafts. Give them the first chance to
    finish, while network workers continue running concurrently. Do not create
    a local pool solely for shutdown if the application has never used one.
    """
    from PySide6.QtCore import QThreadPool
    from .ui import worker

    pools = ([worker._local_pool] if worker._local_pool is not None else [])
    pools.append(QThreadPool.globalInstance())
    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    finished = True
    for pool in pools:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if not pool.waitForDone(remaining):
            finished = False
    return finished


# Удаление в Gmail через антивирус, проверяющий соединение, или в ящике, занятом проверкой
# почты, идёт десятки секунд: столько и ждём, с окном «Выйти сейчас».
MAIL_OPERATIONS_TIMEOUT_MS = 60000
_MAIL_DIALOG_DELAY = 0.5


def wait_for_mail_operations(events, timeout_ms: int = MAIL_OPERATIONS_TIMEOUT_MS) -> bool:
    """Дождаться операций с письмами, начатых до выхода (письма уже убраны из списка).

    Если оборвать их, сервер мог уже перенести письмо в корзину, а в базе оно ещё во
    «Входящих»: после запуска оно оказалось бы в «Локальном архиве» и дублем в корзине.
    Окно с кнопкой «Выйти сейчас» появляется, только если ждать приходится заметно.
    """
    events = [event for event in events if not event.is_set()]
    if not events:
        return True
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QProgressDialog
    from .i18n import tr

    started = time.monotonic()
    deadline = started + max(0, timeout_ms) / 1000
    dialog = None
    try:
        while True:
            pending = next((event for event in events if not event.is_set()), None)
            now = time.monotonic()
            if pending is None or now >= deadline or (dialog is not None and dialog.wasCanceled()):
                break
            if dialog is None and now - started >= _MAIL_DIALOG_DELAY and QApplication.instance() is not None:
                dialog = QProgressDialog(tr("Завершаются операции с письмами на сервере…"), tr("Выйти сейчас"), 0, 0)
                dialog.setWindowTitle("SimpleMail")
                dialog.setWindowModality(Qt.ApplicationModal)
                dialog.setMinimumDuration(0)
                dialog.show()
            if QApplication.instance() is not None:
                QApplication.processEvents()
            pending.wait(0.05)
    finally:
        if dialog is not None:
            dialog.close()
    return all(event.is_set() for event in events)


def force_exit(code: int) -> None:
    """Exit after saved state/logs are flushed, bypassing Windows DLL detach.

    Windows CRT _exit still invokes DLL detach handlers. PySide can access
    already torn-down thread state there if a network worker is still alive.
    Terminate only our own process, preserving the intended exit status.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        if not kernel.TerminateProcess(kernel.GetCurrentProcess(), code):
            raise ctypes.WinError(ctypes.get_last_error())
    os._exit(code)
