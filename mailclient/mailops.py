"""Безопасное перемещение в корзину и восстановление, без общего EXPUNGE."""
from __future__ import annotations

import imaplib
import re

from .db import Database, Message
from .i18n import tr
from .errors import MailOperationError, explain, server_text
from .protocols import INBOX, _uidvalidity, get_password, imap_connect, imap_mailbox as _mailbox


def _ok(result):
    typ, data = result
    if typ != "OK":
        raise imaplib.IMAP4.error(server_text(data) or tr("сервер не выполнил команду"))
    return data


def _trash_mailbox(im) -> str:
    fallback = None
    for item in _ok(im.list('""', '"*"')) or []:
        literal = item[1] if isinstance(item, tuple) else None
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'^\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)$', line)
        if not match:
            continue
        flags, name = match.groups()
        if b"\\noselect" in flags.lower().split():
            continue
        if literal is not None:
            name = literal
        elif name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
        name = name.decode("utf-8")
        if b"\\trash" in flags.lower().split():
            return name
        if name.casefold() in ("trash", "deleted items", "deleted messages", "inbox.trash", "inbox/trash"):
            fallback = name
    if fallback:
        return fallback
    _ok(im.create(_mailbox("Trash")))
    return "Trash"


def _selected_uid(im, db: Database, m: Message) -> tuple[int, int]:
    """Проверить поколение папки и наличие конкретного UID до изменения сервера."""
    _ok(im.select(_mailbox(m.server_folder)))
    validity = _uidvalidity(im)
    expected = m.uidvalidity or db.folder_state(m.account_id, m.server_folder)[0]
    if expected is not None and (not validity or expected != validity):
        raise MailOperationError(tr("Серверная папка изменилась. Письмо оставлено без изменений."))
    uid = m.uid
    if uid is None:
        if not m.message_id or not m.raw:
            raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
        header = _mailbox(m.message_id)
        data = _ok(im.uid("SEARCH", None, "HEADER", "Message-ID", header))
        candidates = []
        for token in (data[0] or b"").split():
            if not token.isdigit():
                continue
            fetched = _ok(im.uid("FETCH", token.decode(), "(UID BODY.PEEK[])"))
            if any(isinstance(item, tuple) and item[1] == m.raw for item in fetched):
                candidates.append(int(token))
        if len(candidates) != 1:
            raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
        uid = candidates[0]
    else:
        fields = "(UID)" if m.uidvalidity is not None else "(UID BODY.PEEK[])"
        fetched = _ok(im.uid("FETCH", str(uid), fields))
        found = False
        for item in fetched:
            meta = item[0] if isinstance(item, tuple) else item
            if isinstance(meta, bytes) and re.search(rb"\bUID " + str(uid).encode() + rb"\b", meta):
                if m.uidvalidity is not None or (m.raw and isinstance(item, tuple) and item[1] == m.raw):
                    found = True
        if not found:
            raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
    return uid, validity


def _copyuid(im, source_uid: int) -> tuple[int | None, int | None]:
    _, data = im.response("COPYUID")
    for item in data or []:
        if not isinstance(item, bytes):
            continue
        parts = item.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            validity, source, destination = map(int, parts)
            if source == source_uid and validity > 0 and destination > 0:
                return destination, validity
    return None, None


def _server_move(db: Database, m: Message, destination: str | None) -> dict:
    acc = db.account(m.account_id)
    im = imap_connect(acc, get_password(db, acc.id))
    try:
        caps = {token.upper() for line in _ok(im.capability()) for token in line.split()}
        if b"MOVE" not in caps and b"UIDPLUS" not in caps:
            raise MailOperationError(tr("Сервер не поддерживает безопасное перемещение писем. Письмо оставлено на месте."))
        uid, validity = _selected_uid(im, db, m)
        destination = destination or _trash_mailbox(im)
        if destination == m.server_folder:
            return dict(server_folder=destination, uid=uid, uidvalidity=validity)
        if b"MOVE" in caps:
            _ok(im.uid("MOVE", str(uid), _mailbox(destination)))
            new_uid, new_validity = _copyuid(im, uid)
        else:
            _ok(im.uid("COPY", str(uid), _mailbox(destination)))
            new_uid, new_validity = _copyuid(im, uid)
            _ok(im.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)"))
            try:
                _ok(im.uid("EXPUNGE", str(uid)))
            except Exception:
                try:
                    im.uid("STORE", str(uid), "-FLAGS.SILENT", "(\\Deleted)")
                except Exception:
                    pass
                raise
        return dict(server_folder=destination, uid=new_uid, uidvalidity=new_validity)
    finally:
        try:
            im.logout()
        except Exception:
            pass


def move_to_trash(db: Database, message_id: int) -> int:
    m = db.message(message_id)
    if not m:
        return message_id
    with db.account_lock(m.account_id):
        m = db.message(message_id)
        if not m or m.folder == "trash":
            return message_id
        fields = _server_move(db, m, None) if m.server_folder else {}
        db.update_message(m.id, folder="trash", trash_origin_folder=m.folder,
                          trash_origin_server_folder=m.server_folder, **fields)
    return message_id


def change_messages(db: Database, ids: list[int], operation) -> tuple[list[int], list]:
    """Ошибки одного письма не прерывают обработку остальных выбранных писем."""
    done, errors = [], []
    for mid in ids:
        try:
            operation(db, mid)
        except Exception as error:
            errors.append(explain(error))
        else:
            done.append(mid)
    return done, errors


def set_spam_state(db: Database, message_id: int, is_spam: bool) -> int:
    from .spam import SpamFilter
    m = db.message(message_id)
    if not m:
        return message_id
    with db.account_lock(m.account_id):
        m = db.message(message_id)
        if not m or m.folder not in ("inbox", "spam"):
            return message_id
        destination = None
        if is_spam:
            destination = next((row["name"] for row in db.server_folders(m.account_id) if row["view"] == "spam"), None)
        elif m.folder == "spam":
            destination = INBOX
        fields = _server_move(db, m, destination) if m.server_folder and destination else {}
        SpamFilter(db).train(m, is_spam)
        db.update_message(m.id, folder="spam" if is_spam and destination else "inbox", is_spam=int(is_spam),
                          spam_reason="вручную: спам" if is_spam else "вручную: не спам", **fields)
    return message_id


def restore_message(db: Database, message_id: int) -> int:
    m = db.message(message_id)
    if not m:
        return message_id
    with db.account_lock(m.account_id):
        m = db.message(message_id)
        if not m or m.folder != "trash":
            return message_id
        if m.trash_origin_folder and m.server_folder:
            fields = _server_move(db, m, m.trash_origin_server_folder or INBOX)
        else:
            # Ранее удалённую локальную копию восстанавливаем только локально.
            fields = dict(server_folder=None, uid=None, uidvalidity=None)
        db.update_message(m.id, folder=m.trash_origin_folder or "inbox",
                          trash_origin_folder=None, trash_origin_server_folder=None, **fields)
    return message_id


def delete_permanently(db: Database, message_id: int) -> int:
    m = db.message(message_id)
    if not m:
        return message_id
    with db.account_lock(m.account_id):
        m = db.message(message_id)
        if not m or m.folder != "trash":
            return message_id
        if m.trash_origin_folder and m.server_folder:
            acc = db.account(m.account_id)
            im = imap_connect(acc, get_password(db, acc.id))
            try:
                caps = {token.upper() for line in _ok(im.capability()) for token in line.split()}
                if b"UIDPLUS" not in caps:
                    raise MailOperationError(tr("Сервер не поддерживает безопасное окончательное удаление. Письмо оставлено в корзине."))
                uid, _ = _selected_uid(im, db, m)
                _ok(im.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)"))
                try:
                    _ok(im.uid("EXPUNGE", str(uid)))
                except Exception:
                    try:
                        im.uid("STORE", str(uid), "-FLAGS.SILENT", "(\\Deleted)")
                    except Exception:
                        pass
                    raise
            finally:
                try:
                    im.logout()
                except Exception:
                    pass
        db.delete_message(m.id)
    return message_id
