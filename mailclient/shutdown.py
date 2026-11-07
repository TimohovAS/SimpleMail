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
