"""Работа с серверами: IMAP (приём) и SMTP (отправка)."""
from __future__ import annotations

from .i18n import tr

import base64
import imaplib
import mimetypes
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .db import Account, Database, Message
from .errors import ErrorInfo, MailOperationError, explain
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
    return results


def friendly_error(e: Exception) -> str:
    return explain(e).short()


def sync_account(db: Database, acc: Account, progress=lambda text: None, full_history=False, cancel=None) -> int:
    """Синхронизировать все папки, флаги и очередь исходящих одного ящика."""
    from .sync import synchronize
    # Ящик блокируется не на всю проверку, а по шагам внутри synchronize():
    # отправка, удаление и пометки не ждут окончания долгой синхронизации.
    with db.sync_lock(acc.id):
        requested = full_history or db.get_setting(f"history:{acc.id}") in ("running", "complete")
        return synchronize(db, acc, progress, requested, cancel)


def _uidvalidity(im: imaplib.IMAP4) -> int:
    typ, data = im.response("UIDVALIDITY")
    try:
        return int(data[0])
    except Exception:
        return 0


def set_server_flag(db: Database, msg: Message, flag: str, add: bool = True) -> None:
    """Установить/снять флаг (\\Seen, \\Deleted) на сервере."""
    with db.account_lock(msg.account_id):
        current = db.message(msg.id) if msg.id is not None else msg
        if current:
            _set_server_flag(db, current, flag, add)
            if flag == "\\Seen" and current.id is not None:
                with db.conn() as c:
                    c.execute("UPDATE messages SET pending_seen=NULL WHERE id=? AND pending_seen=?", (current.id, int(add)))


def _set_server_flag(db: Database, msg: Message, flag: str, add: bool) -> None:
    if msg.uid is None or not msg.server_folder:
        return
    acc = db.account(msg.account_id)
    im = imap_connect(acc, get_password(db, acc.id))
    try:
        typ, data = im.select(imap_mailbox(msg.server_folder))
        if typ != "OK":
            raise imaplib.IMAP4.error(str(data))
        expected = msg.uidvalidity or db.folder_state(msg.account_id, msg.server_folder)[0]
        if expected is not None and _uidvalidity(im) != expected:
            raise MailOperationError(tr("Серверная папка изменилась. Письмо оставлено без изменений."))
        if msg.uidvalidity is None:
            typ, data = im.uid("FETCH", str(msg.uid), "(UID BODY.PEEK[])")
            if typ != "OK" or not msg.raw or not any(isinstance(item, tuple) and item[1] == msg.raw for item in data):
                raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
        typ, data = im.uid("STORE", str(msg.uid), "+FLAGS" if add else "-FLAGS", f"({flag})")
        if typ != "OK":
            raise imaplib.IMAP4.error(str(data))
    finally:
        try:
            im.logout()
        except Exception:
            pass


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
    em["Subject"] = subject
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=acc.email.split("@")[-1] or None)
    if in_reply_to:
        em["In-Reply-To"] = in_reply_to
        em["References"] = in_reply_to
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
