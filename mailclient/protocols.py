"""Работа с серверами: IMAP (приём) и SMTP (отправка)."""
from __future__ import annotations

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
from .errors import ErrorInfo, explain
from .mailparse import parse_message
from .passwords import get_password
from .spam import SpamFilter

INBOX = "INBOX"
FETCH_BATCH = 50
TIMEOUT = 30


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
    for name, connect, close in (("Приём почты (IMAP)", imap_connect, lambda c: c.logout()),
                                 ("Отправка почты (SMTP)", smtp_connect, lambda c: c.quit())):
        try:
            close(connect(acc, password))
            results.append(CheckResult(name, True))
        except Exception as e:
            results.append(CheckResult(name, False, explain(e)))
    return results


def friendly_error(e: Exception) -> str:
    return explain(e).short()


def sync_account(db: Database, acc: Account, progress=lambda text: None) -> int:
    """Скачивает новые письма из INBOX, прогоняет через спам-фильтр. Возвращает число новых писем."""
    password = get_password(db, acc.id)
    spam = SpamFilter(db)
    im = imap_connect(acc, password)
    try:
        typ, data = im.select(INBOX, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Не удалось открыть {INBOX}: {data}")
        uidvalidity = _uidvalidity(im)
        saved_validity, last_uid = db.folder_state(acc.id, INBOX)
        if saved_validity is not None and saved_validity != uidvalidity:
            last_uid = 0  # сервер пересоздал папку — качаем заново (дубли отсекутся по uid)

        typ, data = im.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid]
        if last_uid == 0:
            limit = int(db.get_setting("initial_fetch_limit", "300"))
            uids = uids[-limit:]

        new_count = 0
        for i in range(0, len(uids), FETCH_BATCH):
            batch = uids[i:i + FETCH_BATCH]
            progress(f"{acc.name}: загрузка {i + len(batch)} из {len(uids)}")
            typ, data = im.uid("FETCH", ",".join(map(str, batch)), "(UID FLAGS BODY.PEEK[])")
            for item in data:
                if not isinstance(item, tuple):
                    continue
                meta, raw = item[0].decode(errors="replace"), item[1]
                uid = int(re.search(r"UID (\d+)", meta).group(1))
                m = parse_message(raw)
                m.account_id = acc.id
                m.folder = "inbox"
                m.server_folder = INBOX
                m.uid = uid
                m.seen = "\\Seen" in meta
                v = spam.classify(m)
                m.is_spam, m.spam_score, m.spam_reason = v.is_spam, v.score, v.reason
                if db.add_message(m):
                    new_count += 1
                last_uid = max(last_uid, uid)
            db.set_folder_state(acc.id, INBOX, uidvalidity, last_uid)
        db.set_folder_state(acc.id, INBOX, uidvalidity, last_uid)
        return new_count
    finally:
        try:
            im.logout()
        except Exception:
            pass


def _uidvalidity(im: imaplib.IMAP4) -> int:
    typ, data = im.response("UIDVALIDITY")
    try:
        return int(data[0])
    except Exception:
        return 0


def set_server_flag(db: Database, msg: Message, flag: str, add: bool = True) -> None:
    """Установить/снять флаг (\\Seen, \\Deleted) на сервере."""
    if msg.uid is None or not msg.server_folder:
        return
    acc = db.account(msg.account_id)
    im = imap_connect(acc, get_password(db, acc.id))
    try:
        im.select(msg.server_folder)
        im.uid("STORE", str(msg.uid), "+FLAGS" if add else "-FLAGS", f"({flag})")
        if flag == "\\Deleted" and add:
            im.expunge()
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


def send_message(db: Database, acc: Account, em: EmailMessage) -> None:
    s = smtp_connect(acc, get_password(db, acc.id))
    try:
        s.send_message(em)
    finally:
        try:
            s.quit()
        except Exception:
            pass
    raw = em.as_bytes()
    m = parse_message(raw)
    m.account_id = acc.id
    m.folder = "sent"
    m.seen = True
    m.date_ts = m.date_ts or int(time.time())
    db.add_message(m)
