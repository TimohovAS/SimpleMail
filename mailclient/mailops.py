"""Безопасное перемещение в корзину и восстановление, без общего EXPUNGE."""
from __future__ import annotations

import imaplib
import json
import re
import time
from contextlib import nullcontext

from .db import Database, Message
from .i18n import tr
from .errors import MailOperationError, explain, server_text
from .protocols import INBOX, _uidvalidity, account_imap, get_password, imap_connect, imap_mailbox as _mailbox, imap_quote, uses_utf8, encode_mailbox


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
        if uses_utf8(im):
            name = encode_mailbox(name)
        if b"\\trash" in flags.lower().split():
            return name
        if name.casefold() in ("trash", "deleted items", "deleted messages", "inbox.trash", "inbox/trash"):
            fallback = name
    if fallback:
        return fallback
    _ok(im.create(_mailbox("Trash", im)))
    return "Trash"


def _selected_uid(im, db: Database, m: Message) -> tuple[int, int]:
    """Проверить поколение папки и наличие конкретного UID до изменения сервера."""
    _ok(im.select(_mailbox(m.server_folder, im)))
    validity = _uidvalidity(im)
    expected = m.uidvalidity or db.folder_state(m.account_id, m.server_folder)[0]
    if expected is not None and (not validity or expected != validity):
        raise MailOperationError(tr("Серверная папка изменилась. Письмо оставлено без изменений."))
    uid = m.uid
    if uid is None:
        if not m.message_id or not m.raw:
            raise MailOperationError(tr("Не удалось однозначно найти письмо на сервере. Локальная копия сохранена."))
        header = imap_quote(m.message_id)
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


def _server_move(db: Database, m: Message, destination: str | None, connection=None) -> dict:
    acc = db.account(m.account_id)
    with (nullcontext(connection) if connection is not None else account_imap(db, acc, imap_connect)) as im:
        caps = {token.upper() for line in _ok(im.capability()) for token in line.split()}
        if b"MOVE" not in caps and b"UIDPLUS" not in caps:
            raise MailOperationError(tr("Сервер не поддерживает безопасное перемещение писем. Письмо оставлено на месте."))
        uid, validity = _selected_uid(im, db, m)
        if not destination:
            # Подключение переиспользуется: корзину ищем через LIST один раз на подключение.
            destination = getattr(im, "_simplemail_trash", None) or _trash_mailbox(im)
            try:
                im._simplemail_trash = destination
            except AttributeError:
                pass
        if destination == m.server_folder:
            return dict(server_folder=destination, uid=uid, uidvalidity=validity)
        if b"MOVE" in caps:
            _ok(im.uid("MOVE", str(uid), _mailbox(destination, im)))
            new_uid, new_validity = _copyuid(im, uid)
        else:
            _ok(im.uid("COPY", str(uid), _mailbox(destination, im)))
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


def _complete_for_action(db: Database, message: Message) -> Message:
    """Keep a full local copy before a move can discard the only known UID.

    A server is allowed to omit COPYUID even after a successful MOVE. Our
    fallback then matches full bytes; headers alone cannot identify that copy.
    """
    if getattr(message, "body_downloaded", True):
        return message
    from .sync import fetch_message_body
    complete = fetch_message_body(db, message.id)
    if complete is None or not getattr(complete, "body_downloaded", True) or not complete.raw:
        raise MailOperationError(tr("Содержимое письма ещё не загружено. Повторите действие после загрузки."))
    return complete


def move_to_trash(db: Database, message_id: int) -> int:
    m = db.message(message_id)
    if not m:
        return message_id
    with db.account_lock(m.account_id):
        m = db.message(message_id)
        if not m or m.folder == "trash":
            return message_id
        if m.server_folder:
            m = _complete_for_action(db, m)
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
        m = _complete_for_action(db, m)
        destination = None
        if is_spam:
            destination = next((row["name"] for row in db.server_folders(m.account_id) if row["view"] == "spam"), None)
        elif m.folder == "spam":
            destination = INBOX
        fields = _server_move(db, m, destination) if m.server_folder and destination else {}
        SpamFilter(db).train(m, is_spam)
        db.update_message(m.id, folder="spam" if is_spam and destination else "inbox", is_spam=int(is_spam),
                          spam_reason="вручную: спам" if is_spam else "вручную: не спам", **fields)
        if is_spam:
            db.forget_spam_sender(m.sender)
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
            m = _complete_for_action(db, m)
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
            with account_imap(db, acc, imap_connect) as im:
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
        db.delete_message(m.id)
    return message_id


# ---------- автоочистка корзины ----------
# Срок и время попадания писем в корзину хранятся в таблице settings: схема базы не меняется.
TRASH_DAYS_MAX = 3650
_PURGE_BATCH = 200


def trash_days(db: Database, account_id: int) -> int:
    """Через сколько дней удалять письма из корзины; 0 — не удалять."""
    try:
        days = int(db.get_setting(f"trash_days:{account_id}", "0") or 0)
    except ValueError:
        return 0
    return min(max(days, 0), TRASH_DAYS_MAX)


def set_trash_days(db: Database, account_id: int, days: int) -> None:
    db.set_setting(f"trash_days:{account_id}", min(max(int(days), 0), TRASH_DAYS_MAX))


def _trash_since(db: Database, account_id: int) -> dict[int, int]:
    try:
        data = json.loads(db.get_setting(f"trash_since:{account_id}") or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        if str(key).isdigit() and type(value) is int:
            result[int(key)] = value
    return result


def track_trash(db: Database, account_id: int, now: float | None = None) -> dict[int, int]:
    """Запомнить, когда письмо впервые замечено в корзине (при проверке почты).
    Возвращает {id письма: время} для всех писем, которые сейчас в корзине."""
    now = int(time.time() if now is None else now)
    known = _trash_since(db, account_id)
    ids = [r[0] for r in db.conn().execute(
        "SELECT id FROM messages WHERE account_id=? AND folder='trash'", (account_id,))]
    stamps = {mid: known.get(mid, now) for mid in ids}
    if stamps != known:
        db.set_setting(f"trash_since:{account_id}", json.dumps({str(k): v for k, v in sorted(stamps.items())}))
    return stamps


def purge_old_trash(db: Database, acc, im, now: float | None = None, cancel=None) -> int:
    """Окончательно удалить письма, пролежавшие в корзине дольше срока из настроек ящика.

    Возраст считается с момента, когда письмо оказалось в корзине (SimpleMail замечает это
    при проверке почты), а не по дате письма. Удаляются только выбранные UID (UID EXPUNGE),
    другие письма с флагом \\Deleted не затрагиваются. Без UIDPLUS сервер не трогаем.
    Возвращает число удалённых писем."""
    now = time.time() if now is None else now
    lock = db.account_lock(acc.id)
    with lock:
        stamps = track_trash(db, acc.id, now)
        days = trash_days(db, acc.id)
        if days <= 0 or not stamps:
            return 0
        limit = now - days * 86400
        due = [mid for mid, ts in stamps.items() if ts <= limit]
        if not due:
            return 0
        rows = []
        for start in range(0, len(due), 500):
            part = due[start:start + 500]
            rows += db.conn().execute(
                f"SELECT id, server_folder, uid, uidvalidity, trash_origin_folder FROM messages "
                f"WHERE folder='trash' AND id IN ({','.join('?' * len(part))})", part).fetchall()
    local, by_folder = [], {}
    for r in rows:
        if r["trash_origin_folder"] and r["server_folder"]:
            if r["uid"] is not None:  # без UID письмо однозначно не найти: оставляем как есть
                by_folder.setdefault(r["server_folder"], []).append(r)
        else:
            local.append(r["id"])  # как в delete_permanently: только локальная копия
    removed = 0
    with lock:
        for mid in local:
            m = db.message(mid, with_raw=False)
            if m and m.folder == "trash":
                db.delete_message(mid)
                removed += 1
    if not by_folder:
        return removed
    caps = {token.upper() for line in _ok(im.capability()) for token in (line or b"").split()}
    if b"UIDPLUS" not in caps:
        return removed  # общий EXPUNGE удалил бы чужие письма с \Deleted
    for folder, items in by_folder.items():
        for start in range(0, len(items), _PURGE_BATCH):
            if cancel and cancel.is_set():
                return removed
            with lock:
                _ok(im.select(_mailbox(folder, im)))
                validity = _uidvalidity(im)
                expected = db.folder_state(acc.id, folder)[0]
                batch = []
                for r in items[start:start + _PURGE_BATCH]:
                    current = db.message(r["id"], with_raw=False)
                    if not current or current.folder != "trash" or current.server_folder != folder:
                        continue  # письмо восстановили или удалили, пока ждали
                    want = current.uidvalidity or expected
                    if validity and want == validity and current.uid is not None:
                        batch.append(current)
                if not batch:
                    continue
                uids = ",".join(str(m.uid) for m in batch)
                _ok(im.uid("STORE", uids, "+FLAGS.SILENT", "(\\Deleted)"))
                try:
                    _ok(im.uid("EXPUNGE", uids))
                except Exception:
                    try:
                        im.uid("STORE", uids, "-FLAGS.SILENT", "(\\Deleted)")
                    except Exception:
                        pass
                    raise
                for m in batch:
                    db.delete_message(m.id)
                removed += len(batch)
            lock.wait_uncontended()
    with lock:
        track_trash(db, acc.id, now)
    return removed
