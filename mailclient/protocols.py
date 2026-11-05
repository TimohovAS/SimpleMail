"""Работа с серверами: IMAP (приём) и SMTP (отправка)."""
from __future__ import annotations

from .i18n import tr

import atexit
import base64
import hashlib
import imaplib
import threading
from contextlib import contextmanager
import mimetypes
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .db import Account, Database, Message
from .errors import ErrorInfo, MailOperationError, explain, server_text
from .mailparse import parse_message
from .passwords import get_password
from .spam import SpamFilter

INBOX = "INBOX"
FETCH_BATCH = 20  # писем за один FETCH: меньше пачка — быстрее ящик уступает действиям пользователя
TIMEOUT = 30


def imap_mailbox(name: str) -> bytes:
    """Экранировать имя папки, включая пробелы и кавычки в LIST-ответах."""
    if any(char in name for char in ("\r", "\n", "\0")):
        raise ValueError(tr("Недопустимое имя серверной папки"))
    return b'"' + name.encode("utf-8").replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def imap_connect(acc: Account, password: str) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if acc.imap_security == "ssl":
        im = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port, ssl_context=ctx, timeout=TIMEOUT)
    else:
        im = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=TIMEOUT)
        if acc.imap_security == "starttls":
            im.starttls(ssl_context=ctx)
    try:
        imap_login(im, acc.username, password)
    except Exception:
        try:
            im.shutdown()
        except Exception:
            pass
        raise
    return im


# ---------- общее подключение для действий пользователя ----------
# Удаление, перемещение, пометки выполняются под блокировкой ящика и раньше каждый раз
# заново подключались и входили (удаление 50 писем = 50 входов). Теперь подключение
# переиспользуется, пока им пользуются не реже раза в REUSE_SECONDS.
REUSE_SECONDS = 60
_pool: dict[int, tuple[tuple, imaplib.IMAP4, float]] = {}
_pool_guard = threading.Lock()


def _close_quietly(im) -> None:
    try:
        im.logout()
    except Exception:
        try:
            im.shutdown()
        except Exception:
            pass


def _pool_key(acc: Account, password: str, connect) -> tuple:
    digest = hashlib.sha256(password.encode("utf-8", "surrogatepass")).hexdigest()
    return (acc.imap_host, acc.imap_port, acc.imap_security, acc.username, digest, id(connect))


@contextmanager
def account_imap(db: Database, acc: Account, connect=None):
    """IMAP-подключение ящика для одной операции; после успеха возвращается в запас.

    Вызывать под db.account_lock(acc.id): подключение не используется двумя потоками сразу.
    connect — функция подключения (по умолчанию imap_connect этого модуля)."""
    connect = connect or imap_connect
    password = get_password(db, acc.id)
    key = _pool_key(acc, password, connect)
    now = time.monotonic()
    stale = []
    with _pool_guard:
        entry = _pool.pop(acc.id, None)
        for other, (_, other_im, used) in list(_pool.items()):
            if now - used > REUSE_SECONDS:
                stale.append(_pool.pop(other)[1])
    im = None
    if entry:
        old_key, old_im, used = entry
        if old_key == key and now - used <= REUSE_SECONDS:
            im = old_im
        else:
            stale.append(old_im)
    for old in stale:
        _close_quietly(old)
    if im is None:
        im = connect(acc, password)
    try:
        yield im
    except BaseException:
        _close_quietly(im)  # состояние подключения после ошибки неизвестно
        raise
    with _pool_guard:
        previous = _pool.pop(acc.id, None)
        _pool[acc.id] = (key, im, time.monotonic())
    if previous:
        _close_quietly(previous[1])


@atexit.register
def close_pool() -> None:
    with _pool_guard:
        items = list(_pool.values())
        _pool.clear()
    for _, im, _ in items:
        _close_quietly(im)


def imap_login(im: imaplib.IMAP4, username: str, password: str) -> None:
    """LOGIN для латиницы; для пароля/логина с другими символами — AUTHENTICATE PLAIN в UTF-8."""
    if (username + password).isascii():
        im.login(username, password)
    else:
        token = b"\0" + username.encode("utf-8") + b"\0" + password.encode("utf-8")
        im.authenticate("PLAIN", lambda _: token)


@dataclass
class CheckResult:
    name: str                    # «Приём почты (IMAP)»
    ok: bool
    error: ErrorInfo | None = None
    note: str = ""               # совет (например, сервер умеет шифрование)


def test_connection(acc: Account, password: str) -> list[CheckResult]:
    """Проверка приёма и отправки по отдельности. Не бросает исключений."""
    results = []
    for name, connect, close in ((tr("Приём почты (IMAP)"), imap_connect, lambda c: c.logout()),
                                 (tr("Отправка почты (SMTP)"), smtp_connect, lambda c: c.quit())):
        try:
            close(connect(acc, password))
            results.append(CheckResult(name, True))
        except Exception as e:
            results.append(CheckResult(name, False, explain(e)))
    # Без шифрования пароль идёт по сети открытым текстом: подсказываем, если сервер умеет STARTTLS.
    for result, security, probe in ((results[0], acc.imap_security, _imap_offers_starttls),
                                    (results[1], acc.smtp_security, _smtp_offers_starttls)):
        if result.ok and security == "none":
            try:
                if probe(acc):
                    result.note = tr("Сервер поддерживает шифрование STARTTLS. Выберите его, чтобы пароль и письма не передавались открытым текстом.")
            except Exception:
                pass
    return results


def _imap_offers_starttls(acc: Account) -> bool:
    im = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=TIMEOUT)
    try:
        return "STARTTLS" in im.capabilities
    finally:
        _close_quietly(im)


def _smtp_offers_starttls(acc: Account) -> bool:
    s = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=TIMEOUT)
    try:
        s.ehlo()
        return s.has_extn("starttls")
    finally:
        try:
            s.quit()
        except Exception:
            s.close()


def friendly_error(e: Exception) -> str:
    return explain(e).short()


def sync_account(db: Database, acc: Account, progress=lambda text: None, full_history=False, cancel=None,
                 history_cancel=None, inbox_only=False) -> int:
    """cancel останавливает всю проверку, history_cancel — только загрузку старых писем."""
    from .sync import synchronize
    # Ящик блокируется не на всю проверку, а по шагам внутри synchronize():
    # отправка, удаление и пометки не ждут окончания долгой синхронизации.
    with db.sync_lock(acc.id):
        requested = full_history or db.get_setting(f"history:{acc.id}") in ("running", "complete")
        return synchronize(db, acc, progress, requested, cancel, history_cancel, inbox_only)


def _uidvalidity(im: imaplib.IMAP4) -> int:
    typ, data = im.response("UIDVALIDITY")
    try:
        return int(data[0])
    except Exception:
        return 0


def set_server_flag(db: Database, msg: Message, flag: str, add: bool = True, *, queued=False) -> None:
    """Установить/снять флаг (\\Seen, \\Deleted) на сервере."""
    with db.account_lock(msg.account_id):
        current = db.message(msg.id) if msg.id is not None else msg
        if current:
            column = {"\\Seen": "pending_seen", "\\Flagged": "pending_flagged"}.get(flag)
            if queued and column:
                pending = getattr(current, column)
                if pending is None:
                    return  # более новая задача уже отправила последнее намерение пользователя
                add = bool(pending)
            _set_server_flag(db, current, flag, add)
            if column and current.id is not None:
                with db.conn() as c:
                    c.execute(f"UPDATE messages SET {column}=NULL WHERE id=? AND {column}=?", (current.id, int(add)))


def _set_server_flag(db: Database, msg: Message, flag: str, add: bool) -> None:
    if msg.uid is None or not msg.server_folder:
        return
    acc = db.account(msg.account_id)
    with account_imap(db, acc) as im:
        typ, data = im.select(imap_mailbox(msg.server_folder))
        if typ != "OK":
            raise imaplib.IMAP4.error(server_text(data) or tr("сервер не выполнил команду"))
        expected = msg.uidvalidity or db.folder_state(msg.account_id, msg.server_folder)[0]
        if expected is not None and _uidvalidity(im) != expected:
            raise MailOperationError(tr("Серверная папка изменилась. Письмо оставлено без изменений."))
        if msg.uidvalidity is None:
            typ, data = im.uid("FETCH", str(msg.uid), "(UID BODY.PEEK[])")
            if typ != "OK" or not msg.raw or not any(isinstance(item, tuple) and item[1] == msg.raw for item in data):
                raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
        typ, data = im.uid("STORE", str(msg.uid), "+FLAGS" if add else "-FLAGS", f"({flag})")
        if typ != "OK":
            raise imaplib.IMAP4.error(server_text(data) or tr("сервер не выполнил команду"))


# ---------------- SMTP ----------------

def smtp_connect(acc: Account, password: str) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if acc.smtp_security == "ssl":
        s = smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port, context=ctx, timeout=TIMEOUT)
    else:
        s = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=TIMEOUT)
        if acc.smtp_security == "starttls":
            s.starttls(context=ctx)
    try:
        smtp_login(s, acc.username, password)
    except Exception:
        s.close()
        raise
    return s


def smtp_login(s: smtplib.SMTP, username: str, password: str) -> None:
    s.ehlo_or_helo_if_needed()
    if not s.has_extn("auth"):
        return  # сервер не требует авторизации (например, внутренний релей)
    if (username + password).isascii():
        s.login(username, password)
        return
    # smtplib кодирует пароль только в ASCII — делаем AUTH PLAIN в UTF-8 сами
    token = base64.b64encode(b"\0" + username.encode("utf-8") + b"\0" + password.encode("utf-8")).decode()
    code, resp = s.docmd("AUTH", "PLAIN " + token)
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, resp)


def build_message(acc: Account, to: str, subject: str, body: str, cc: str = "",
                  in_reply_to: str = "", attachments: list[tuple[str, bytes]] | None = None,
                  references: str = "", bcc: str = "",
                  html: str | None = None,
                  inline_images: list[tuple[str, str, bytes]] | None = None) -> EmailMessage:
    """Собрать письмо.

    body — обычный текст (всегда есть, для почтовиков без HTML);
    html — HTML-версия (необязательно); inline_images — [(cid, mime, данные)] для картинок в тексте.
    Итог: multipart/mixed( multipart/alternative( text/plain, multipart/related(text/html, картинки) ), вложения ).
    """
    em = EmailMessage()
    em["From"] = f"{acc.name} <{acc.email}>" if acc.name and acc.name != acc.email else acc.email
    em["To"] = to
    if cc:
        em["Cc"] = cc
    if bcc:
        # smtplib.send_message отправляет скрытым адресатам и вырезает Bcc из передаваемого письма;
        # в копии «Отправленных» заголовок остаётся, как в других почтовых программах.
        em["Bcc"] = bcc
    em["Subject"] = subject
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=acc.email.split("@")[-1] or None)
    if in_reply_to:
        em["In-Reply-To"] = in_reply_to
        # References = вся цепочка исходного письма + его Message-ID (RFC 5322 §3.6.4):
        # иначе другие почтовые программы не собирают переписку в одну ветку.
        chain = [ref for ref in references.split() if ref != in_reply_to]
        em["References"] = " ".join(chain[-20:] + [in_reply_to])
    em.set_content(body)
    if html:
        em.add_alternative(html, subtype="html")
        html_part = em.get_payload()[1]
        for cid, mime, data in inline_images or []:
            maintype, _, subtype = mime.partition("/")
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    for name, data in attachments or []:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        em.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return em


def redirect_message(db: Database, acc: Account, raw: bytes, to: str) -> None:
    """Перенаправить письмо без изменений (RFC 5322 Resent-*): получатель видит исходного отправителя."""
    from email.utils import getaddresses
    rcpts = [a for _, a in getaddresses([to]) if a]
    if not rcpts:
        raise ValueError(tr("не указан адрес получателя"))
    headers = (f"Resent-From: {acc.email}\r\nResent-To: {', '.join(rcpts)}\r\n"
               f"Resent-Date: {formatdate(localtime=True)}\r\n"
               f"Resent-Message-ID: {make_msgid(domain=acc.email.split('@')[-1] or None)}\r\n").encode()
    s = smtp_connect(acc, get_password(db, acc.id))
    try:
        s.sendmail(acc.email, rcpts, headers + raw)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def send_message(db: Database, acc: Account, em: EmailMessage) -> str:
    """Отправка из быстрого ответа также использует постоянную очередь."""
    from .delivery import deliver_outbox
    mid = db.save_composition(acc.id, em.as_bytes(), {}, queued=True)
    result = deliver_outbox(db, mid)
    return result
