"""IMAP IDLE на отдельном соединении; совместимо с Python 3.11+."""
from __future__ import annotations

import imaplib
import logging
import select
import socket
import threading
import time

from .db import Account, Database
from .passwords import get_password
from .protocols import INBOX, imap_connect

log = logging.getLogger(__name__)
IDLE_DURATION = 25 * 60  # RFC 2177: обновлять IDLE чаще, чем раз в 29 минут
RECONNECT_INITIAL = 5


class IdleUnavailable(Exception):
    """Сервер не поддерживает или отклонил IDLE."""


def prepare_idle(im):
    """Убираем скрытую буферизацию перед SELECT/IDLE для select()."""
    # Python <= 3.13 читает через BufferedReader. Он может забрать следующий
    # ответ заранее, хотя select() уже не видит данных в сокете. Начиная с 3.14
    # imaplib ведёт собственный _readbuf, который учитываем в idle_once().
    if not hasattr(im, "_readbuf"):
        im.file.close()
        im.file = im.sock.makefile("rb", buffering=0)


def idle_once(im, stopped: threading.Event, duration=IDLE_DURATION) -> bool:
    """Один цикл IDLE. True — сервер сообщил об изменении числа писем."""
    im.untagged_responses.clear()  # EXISTS из SELECT — исходное число писем
    # У imaplib до Python 3.14 нет публичного IDLE. Эти три внутренних метода
    # оставляют разбор ответов (включая literals и BYE) самому imaplib.
    tag = im._new_tag()
    im.send(tag + b" IDLE\r\n")
    while im._get_response() is not None:
        im._check_bye()
        if im.tagged_commands[tag] is not None:
            try:
                im._command_complete("IDLE", tag)
            except imaplib.IMAP4.error as exc:
                raise IdleUnavailable("IDLE rejected") from exc
            raise IdleUnavailable("IDLE rejected")

    deadline = time.monotonic() + duration
    changed = any(key in im.untagged_responses for key in ("EXISTS", "EXPUNGE", "FETCH"))
    while not stopped.is_set() and not changed:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        buffered = any(getattr(im, "_readbuf", ()))
        pending = getattr(im.sock, "pending", lambda: 0)()
        if not buffered and not pending:
            if not select.select([im.sock], [], [], min(1.0, remaining))[0]:
                continue
        im._get_response()
        im._check_bye()
        changed = any(key in im.untagged_responses for key in ("EXISTS", "EXPUNGE", "FETCH"))
        # Некоторые серверы сами завершают IDLE; нельзя посылать лишний DONE.
        if im.tagged_commands[tag] is not None:
            break
        # Не копим тысячи уведомлений о флагах от других почтовых клиентов.
        for key in ("FETCH", "EXPUNGE", "RECENT", "OK"):
            im.untagged_responses.pop(key, None)

    if im.tagged_commands[tag] is None:
        im.send(b"DONE\r\n")
    typ, _ = im._command_complete("IDLE", tag)
    if typ != "OK":
        raise IdleUnavailable("IDLE rejected")
    # EXISTS может прийти и вместе с подтверждением DONE.
    return changed or any(key in im.untagged_responses for key in ("EXISTS", "EXPUNGE", "FETCH"))


class MailWatcher(threading.Thread):
    """Ожидает уведомлений, после разрыва подключается с задержкой 5–300 с."""

    def __init__(self, db: Database, account: Account, on_change, on_unavailable):
        super().__init__(name=f"imap-idle-{account.id}", daemon=True)
        self.db, self.account = db, account
        self.on_change, self.on_unavailable = on_change, on_unavailable
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._connection = None

    def stop(self):
        self.stopped.set()
        # Прерывает ожидание ответа сервера, в том числе при потере сети.
        with self._lock:
            im = self._connection
            if im is not None:
                try:
                    im.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def run(self):
        delay = RECONNECT_INITIAL
        try:
            while not self.stopped.is_set():
                im = None
                try:
                    latest = self.db.account(self.account.id)
                    if latest is None or not latest.enabled:
                        return
                    self.account = latest
                    im = imap_connect(self.account, get_password(self.db, self.account.id, account=self.account))
                    with self._lock:
                        self._connection = im
                    if self.stopped.is_set():
                        break
                    prepare_idle(im)
                    typ, caps = im.capability()  # возможности после авторизации
                    if typ != "OK":
                        raise imaplib.IMAP4.error("CAPABILITY failed")
                    supported = b"IDLE" in b" ".join(caps).upper().split()
                    if not supported:
                        self.on_unavailable(self.account.id)
                        return  # остаётся обычная проверка по таймеру
                    typ, _ = im.select(INBOX, readonly=True)
                    if typ != "OK":
                        raise imaplib.IMAP4.error("SELECT failed")
                    # Скачиваем письма после запуска и после каждого переподключения.
                    self.on_change(self.account.id)
                    while not self.stopped.is_set():
                        started = time.monotonic()
                        changed = idle_once(im, self.stopped)
                        if self.stopped.is_set():
                            break
                        if time.monotonic() - started >= 30:
                            delay = RECONNECT_INITIAL
                        # Проверка при новых письмах, флагах и удалениях; через 25 минут IDLE просто
                        # продлевается — периодическую сверку делает таймер автопроверки.
                        if changed:
                            self.on_change(self.account.id)
                        # Защита от сервера, который немедленно завершает IDLE.
                        if not changed and self.stopped.wait(1):
                            break
                except IdleUnavailable:
                    if not self.stopped.is_set():
                        self.on_unavailable(self.account.id)
                    return
                except Exception:
                    if not self.stopped.is_set():
                        log.warning("IMAP IDLE: connection lost for account %s; retry in %s s",
                                    self.account.id, delay)
                finally:
                    with self._lock:
                        self._connection = None
                    if im is not None:
                        try:
                            im.shutdown()
                        except OSError:
                            pass
                if self.stopped.wait(delay):
                    break
                delay = min(delay * 2, 300)
        finally:
            self.db.close()  # соединение SQLite принадлежит этому потоку
