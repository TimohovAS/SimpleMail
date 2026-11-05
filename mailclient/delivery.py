"""Постоянная очередь отправки: SMTP и сохранение Sent повторяются раздельно."""
from __future__ import annotations

import email
import imaplib
import smtplib
import time
from email import policy

from . import protocols as wire
from .errors import MailOperationError
from .i18n import tr


def _same_content(first, second):
    def fingerprint(raw):
        em = email.message_from_bytes(raw, policy=policy.default)
        headers = tuple(str(em.get(name, "")) for name in ("Message-ID", "From", "To", "Cc", "Subject"))
        parts = []
        for part in em.walk():
            if part.is_multipart():
                continue
            payload = part.get_payload(decode=True) or b""
            if part.get_content_maintype() == "text":
                payload = payload.replace(b"\r\n", b"\n")
            parts.append((part.get_content_type(), part.get_filename(), str(part.get("Content-ID", "")), payload))
        return headers, parts
    return fingerprint(first) == fingerprint(second)


def append_sent(db, mid):
    from .sync import checked, discover_folders
    m = db.message(mid)
    if not m or m.delivery_state != "append_pending":
        return
    acc = db.account(m.account_id)
    im = wire.imap_connect(acc, wire.get_password(db, acc.id))
    try:
        folders = discover_folders(im)
        sent = next((folder.name for folder in folders if folder.view == "sent"), None)
        if sent is None:
            sent = "Sent"
            checked(im.create(wire.imap_mailbox(sent)))
        checked(im.select(wire.imap_mailbox(sent), readonly=True))
        validity = wire._uidvalidity(im)
        if m.message_id:
            data = checked(im.uid("SEARCH", None, "HEADER", "Message-ID", wire.imap_mailbox(m.message_id)))
            matches = []
            for token in (data[0] or b"").split():
                if not token.isdigit():
                    continue
                data = checked(im.uid("FETCH", token.decode(), "(UID BODY.PEEK[])"))
                if any(isinstance(item, tuple) and _same_content(item[1], m.raw) for item in data):
                    matches.append(int(token))
            if len(matches) == 1:
                db.update_message(mid, server_folder=sent, uid=matches[0], uidvalidity=validity,
                                  delivery_state=None, delivery_error=None)
                return
            if len(matches) > 1:
                raise MailOperationError(tr("На сервере несколько копий отправленного письма. Повторное сохранение остановлено."))
        checked(im.append(wire.imap_mailbox(sent), "(\\Seen)", imaplib.Time2Internaldate(m.date_ts or time.time()), m.raw))
        _, data = im.response("APPENDUID")
        uid, epoch = None, None
        for item in data or []:
            if isinstance(item, bytes) and len(parts := item.split()) == 2 and all(part.isdigit() for part in parts):
                epoch, uid = map(int, parts)
        db.update_message(mid, server_folder=sent if uid else None, uid=uid, uidvalidity=epoch,
                          delivery_state=None, delivery_error=None)
    finally:
        try:
            im.logout()
        except Exception:
            pass


def deliver_outbox(db, mid):
    m = db.message(mid)
    if not m:
        return "sent"
    with db.account_lock(m.account_id):
        m = db.message(mid)
        if not m:
            return "sent"
        if m.folder == "sent" and m.delivery_state == "append_pending":
            try:
                append_sent(db, mid)
            except Exception as error:
                db.update_message(mid, delivery_error=str(error))
            return "sent"
        if m.folder != "outbox" or m.delivery_state != "pending":
            return "uncertain" if m.delivery_state in ("sending", "uncertain") else "sent"
        with db.conn() as c:
            claim = c.execute("UPDATE messages SET delivery_state='sending',delivery_error=NULL WHERE id=? AND folder='outbox' AND delivery_state='pending'", (mid,))
            if claim.rowcount != 1:
                return "uncertain"
        acc = db.account(m.account_id)
        try:
            smtp = wire.smtp_connect(acc, wire.get_password(db, acc.id))
        except Exception as error:
            db.update_message(mid, delivery_state="pending", delivery_error=str(error))
            return "queued"
        try:
            em = email.message_from_bytes(m.raw, policy=policy.default)
            refused = smtp.send_message(em)
            if refused:
                # Остальные адресаты могли получить письмо: не повторяем отправку целиком.
                db.update_message(mid, delivery_state="uncertain", delivery_error=tr("Часть адресатов отклонена. Проверьте доставку перед повторной отправкой."))
                return "uncertain"
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
            db.update_message(mid, delivery_state="pending", delivery_error=str(error))
            return "queued"
        except Exception as error:
            db.update_message(mid, delivery_state="uncertain", delivery_error=str(error))
            return "uncertain"
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        db.update_message(mid, folder="sent", delivery_state="append_pending", delivery_error=None)
        try:
            append_sent(db, mid)
        except Exception as error:
            db.update_message(mid, delivery_error=str(error))
        return "sent"


def process_outbox(db, acc, cancel=None):
    ids = [row[0] for row in db.conn().execute(
        "SELECT id FROM messages WHERE account_id=? AND (folder='outbox' AND delivery_state='pending' OR folder='sent' AND delivery_state='append_pending') ORDER BY id", (acc.id,))]
    for mid in ids:
        if cancel and cancel.is_set():
            break
        deliver_outbox(db, mid)
