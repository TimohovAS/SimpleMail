"""Один процесс на базу: две копии приложения не отправляют одну очередь параллельно."""
import ctypes
import hashlib
import os
from pathlib import Path


def acquire_instance(data_dir):
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        key = hashlib.sha256(str(root).casefold().encode()).hexdigest()
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, "Local\\SimpleMail-" + key)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            return None
        # HANDLE сохраняется вызывающим модулем до завершения процесса.
        return handle
    import fcntl
    stream = (root / ".simplemail.lock").open("a+b")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        return None
    return stream
