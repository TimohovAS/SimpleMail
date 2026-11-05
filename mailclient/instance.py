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


# ---------- повторный запуск: поднять окно уже работающей копии ----------

def socket_name(data_dir) -> str:
    key = hashlib.sha256(str(Path(data_dir).resolve()).casefold().encode()).hexdigest()[:16]
    return "SimpleMail-" + key


def notify_running(data_dir, timeout_ms: int = 1500) -> bool:
    """Попросить работающую копию показать окно. False — она не отвечает (например, ещё закрывается)."""
    from PySide6.QtNetwork import QLocalSocket
    if os.name == "nt":
        try:  # разрешить первой копии вывести окно на передний план
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
    sock = QLocalSocket()
    sock.connectToServer(socket_name(data_dir))
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(b"show\n")
    sock.flush()
    # Подтверждение важно: сервер может закрыться сразу после поднятия окна,
    # и waitForBytesWritten тогда вернёт False, хотя команда уже доставлена.
    ok = (sock.canReadLine() or sock.waitForReadyRead(timeout_ms)) and bytes(sock.readLine()).strip() == b"ok"
    sock.disconnectFromServer()
    return ok


def start_activation_server(data_dir, on_show, parent=None):
    """Слушать просьбы повторно запущенной копии; on_show() вызывается в потоке окна."""
    from PySide6.QtNetwork import QLocalServer
    name = socket_name(data_dir)
    QLocalServer.removeServer(name)  # остаток после аварийного завершения (Linux/macOS)
    server = QLocalServer(parent)

    def accept():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            def received(c=conn):
                if c.canReadLine() and bytes(c.readLine()).strip() == b"show":
                    c.write(b"ok\n")
                    c.flush()
                    on_show()
            conn.readyRead.connect(received)
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable():
                received()

    server.newConnection.connect(accept)
    server.listen(name)
    return server
