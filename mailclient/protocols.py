"""Работа с серверами: IMAP (приём) и SMTP (отправка)."""
from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .db import Account, Database, Message
from .mailparse import parse_message
from .passwords import get_password
from .spam import SpamFilter

INBOX = "INBOX"
FETCH_BATCH = 50
TIMEOUT = 30


def imap_connect(acc: Account, password: str) -> imaplib.IMAP4:
    if acc.imap_ssl:
        im = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    else:
        im = imaplib.IMAP4(acc.imap_host, acc.imap_port, timeout=TIMEOUT)
        try:
            im.starttls(ssl_context=ssl.create_default_context())
        except Exception:
            pass
    im.login(acc.username, password)
    return im


def test_connection(acc: Account, password: str) -> str:
    """Проверка IMAP и SMTP. Возвращает текст результата, бросает исключение при ошибке."""
    im = imap_connect(acc, password)
    im.logout()
    s = smtp_connect(acc, password)
    s.quit()
    return "IMAP и SMTP: подключение успешно"


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
    s.login(acc.username, password)
    return s


def build_message(acc: Account, to: str, subject: str, body: str, cc: str = "",
                  in_reply_to: str = "", attachments: list[tuple[str, bytes]] | None = None) -> EmailMessage:
    em = EmailMessage()
    em["From"] = f"{acc.name} <{acc.email}>" if acc.name else acc.email
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
    for name, data in attachments or []:
        em.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
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
