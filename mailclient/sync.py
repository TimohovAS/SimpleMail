"""Синхронизация серверных папок, флагов и возобновляемая загрузка истории."""
from __future__ import annotations

import base64
import imaplib
import time
import re
from dataclasses import dataclass

from .i18n import tr
from . import protocols as wire
from .mailparse import parse_message
from .protocols import FETCH_BATCH, INBOX, _uidvalidity, get_password, imap_connect, imap_mailbox
from .spam import SpamFilter
from . import storage
from .delivery import _same_content
from .errors import MailOperationError, explain, server_text
from .db import SyncPreempted


def checked(result):
    typ, data = result
    if typ != "OK":
        raise imaplib.IMAP4.error(server_text(data) or tr("сервер не выполнил команду"))
    return data


def display_mailbox(name):
    def decode(match):
        token = match.group(1)
        if not token:
            return "&"
        try:
            return base64.b64decode(token.replace(",", "/") + "=" * (-len(token) % 4)).decode("utf-16-be")
        except (ValueError, UnicodeError):
            return match.group(0)
    return re.sub(r"&([^-]*)-", decode, name)


@dataclass
class ServerFolder:
    name: str
    view: str
    display: str
    flags: set


def discover_folders(im):
    result = []
    for item in checked(im.list('""', '"*"')) or []:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'^\(([^)]*)\)\s+(?:"(?:\\.|[^"])*"|NIL)\s+(.+)$', line)
        if not match:
            continue
        flags, name = match.groups()
        flags = {flag.lower() for flag in flags.split()}
        if b"\\noselect" in flags:
            continue
        if isinstance(item, tuple):
            name = item[1]
        elif name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
        name = name.decode("utf-8")
        lower = name.casefold()
        if lower == "inbox":
            view = "inbox"
        elif b"\\sent" in flags or lower in ("sent", "sent items", "sent messages", "inbox.sent"):
            view = "sent"
        elif b"\\trash" in flags or lower in ("trash", "deleted items", "deleted messages", "inbox.trash"):
            view = "trash"
        elif b"\\junk" in flags or lower in ("junk", "spam", "junk e-mail"):
            view = "spam"
        else:
            view = "remote:" + name
        result.append(ServerFolder(name, view, display_mailbox(name), flags))
    if not any(folder.view == "inbox" for folder in result):
        result.insert(0, ServerFolder(INBOX, "inbox", INBOX, set()))
    return result


def _uid(meta):
    match = re.search(rb"\bUID (\d+)\b", meta)
    return int(match.group(1)) if match else None


def sync_mailbox(db, acc, im, folder, progress, full_history=False, cancel=None, yield_point=None, flags_done=False):
    """Синхронизировать одну папку. yield_point() между шагами может бросить SyncPreempted —
    тогда папка синхронизируется заново, уже скачанные письма не скачиваются повторно.
    flags_done — состояния писем этой папки уже обновлены в этой проверке."""
    outer_yield = yield_point or (lambda: None)
    state = {"flags_done": flags_done}

    def yield_point():
        try:
            outer_yield()
        except SyncPreempted as preempted:
            preempted.flags_done = state["flags_done"]
            preempted.new_count = new_count  # уже сохранённые письма этой попытки
            raise
    checked(im.select(imap_mailbox(folder.name), readonly=True))
    validity = _uidvalidity(im)
    if not validity:
        raise imaplib.IMAP4.error(tr("сервер не сообщил UIDVALIDITY папки, синхронизация невозможна"))
    data = checked(im.uid("SEARCH", None, "ALL"))
    all_uids = sorted({int(token) for token in (data[0] or b"").split() if token.isdigit()})
    uid_set = set(all_uids)
    saved_validity, last_uid = db.folder_state(acc.id, folder.name)
    rows = db.conn().execute("SELECT id,uid,uidvalidity,pending_seen,pending_flagged FROM messages WHERE account_id=? AND server_folder=?", (acc.id, folder.name)).fetchall()
    known, legacy = {}, {}
    for row in sorted(rows, key=lambda item: item["uidvalidity"] != validity):
        same_epoch = row["uidvalidity"] in (None, validity)
        if same_epoch and row["uid"] in uid_set and row["uid"] not in known:
            known[row["uid"]] = row["id"]
            if row["uidvalidity"] is None:
                legacy[row["uid"]] = row["id"]
        else:
            db.update_message(row["id"], server_folder=None, uid=None, uidvalidity=None, folder="archive")
    if saved_validity != validity:
        last_uid = 0
    new = [uid for uid in all_uids if uid not in known]
    if not full_history:
        limit = max(1, int(db.get_setting("initial_fetch_limit", "300")))
        new = ([uid for uid in new if uid > last_uid] if last_uid else new[-limit:])
    # Не присваиваем старой записи поколение папки без проверки её содержимого.
    new = sorted(set(new) | set(legacy))
    new_count = 0
    pending = [row for row in rows if row["uid"] in known and row["uid"] not in legacy
               and (row["pending_seen"] is not None or row["pending_flagged"] is not None)]
    if pending:
        yield_point()
        checked(im.select(imap_mailbox(folder.name)))
        if _uidvalidity(im) != validity:
            raise imaplib.IMAP4.error(tr("папка на сервере изменилась во время проверки, она будет проверена в следующий раз"))
        # Пометки, сделанные без связи с сервером: «Прочитано» и флажок «Важное».
        for row in pending:
            for column, flag in (("pending_seen", "\\Seen"), ("pending_flagged", "\\Flagged")):
                if row[column] is None:
                    continue
                checked(im.uid("STORE", str(row["uid"]), "+FLAGS.SILENT" if row[column] else "-FLAGS.SILENT", f"({flag})"))
                with db.conn() as c:
                    c.execute(f"UPDATE messages SET {column}=NULL WHERE id=? AND {column}=?", (row["id"], row[column]))
    # FLAGS обновляем и у писем, прочитанных или изменённых другим клиентом.
    # Одна команда на всю папку вместо пачек по 50: на больших ящиках это тысячи запросов.
    known_uids = [uid for uid in known if uid not in legacy]
    if known_uids and not flags_done:
        if cancel and cancel.is_set():
            return new_count, False
        yield_point()
        progress(tr("{0} / {1}: обновление состояний {2} из {3}", acc.name, folder.display, len(known_uids), len(known_uids)))
        fetched_flags = checked(im.uid("FETCH", "1:*", "(UID FLAGS)"))
        with db.conn() as c:
            for item in fetched_flags:
                meta = item[0] if isinstance(item, tuple) else item
                if isinstance(meta, bytes) and (uid := _uid(meta)) in known and uid not in legacy:
                    flags = imaplib.ParseFlags(meta)
                    c.execute("UPDATE messages SET seen=coalesce(pending_seen,?),flagged=coalesce(pending_flagged,?),"
                              "uidvalidity=?,folder=? WHERE id=?",
                              (int(b"\\Seen" in flags), int(b"\\Flagged" in flags), validity,
                               "archive" if b"\\Deleted" in flags else folder.view, known[uid]))
        state["flags_done"] = True
    for offset in range(0, len(new), FETCH_BATCH):
        if cancel and cancel.is_set():
            return new_count, False
        yield_point()
        batch = new[offset:offset + FETCH_BATCH]
        progress(tr("{0} / {1}: загрузка {2} из {3}", acc.name, folder.display, offset + len(batch), len(new)))
        fetched = checked(im.uid("FETCH", ",".join(map(str, batch)), "(UID FLAGS INTERNALDATE BODY.PEEK[])"))
        received = set()
        for item in fetched:
            if cancel and cancel.is_set():
                return new_count, False
            if not isinstance(item, tuple) or not isinstance(item[0], bytes):
                continue
            uid = _uid(item[0])
            if uid not in batch:
                continue
            m = parse_message(item[1])
            if not m.date_ts:
                # Нет заголовка Date (или он испорчен): берём время получения письма сервером.
                arrived = imaplib.Internaldate2tuple(item[0])
                if arrived:
                    m.date_ts = int(time.mktime(arrived))
            m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = acc.id, folder.view, folder.name, uid, validity
            m.seen = b"\\Seen" in imaplib.ParseFlags(item[0])
            m.flagged = b"\\Flagged" in imaplib.ParseFlags(item[0])
            if b"\\Deleted" in imaplib.ParseFlags(item[0]):
                m.folder = "archive"
            if folder.view == "inbox":
                verdict = SpamFilter(db).classify(m)
                m.is_spam, m.spam_score, m.spam_reason = verdict.is_spam, verdict.score, verdict.reason
            if folder.view == "trash":
                m.trash_origin_folder, m.trash_origin_server_folder = "inbox", INBOX
            # При перемещении другим клиентом сохраняем прежний локальный ID.
            candidate = None
            if uid in legacy:
                old = db.message(legacy[uid])
                if old and (old.raw == m.raw or _same_content(old.raw, m.raw)):
                    candidate = old
                elif old:
                    db.update_message(old.id, server_folder=None, uid=None, uidvalidity=None, folder="archive")
            if m.message_id:
                for row in db.conn().execute("SELECT id FROM messages WHERE account_id=? AND message_id=? AND server_folder IS NULL AND folder NOT IN ('drafts','outbox')", (acc.id, m.message_id)).fetchall():
                    old = db.message(row["id"])
                    if candidate is None and old and (old.raw == m.raw or _same_content(old.raw, m.raw)):
                        candidate = old
                        break
            if candidate:
                raw_fields = {}
                if candidate.raw != m.raw:
                    if acc.storage_dir:
                        path = storage.eml_path(acc.storage_dir, m.folder, m.date_ts, uid, m.raw, f"{folder.name}:{validity}")
                        storage.write_eml(path, m.raw)
                        raw_fields = dict(raw=None, raw_path=str(path), size=len(m.raw))
                    else:
                        raw_fields = dict(raw=m.raw, raw_path=None, size=len(m.raw))
                db.update_message(candidate.id, folder=m.folder, server_folder=m.server_folder, uid=uid, uidvalidity=validity,
                                  seen=candidate.pending_seen if candidate.pending_seen is not None else int(m.seen),
                                  flagged=candidate.pending_flagged if candidate.pending_flagged is not None else int(m.flagged),
                                  trash_origin_folder=candidate.trash_origin_folder or m.trash_origin_folder,
                                  trash_origin_server_folder=candidate.trash_origin_server_folder or m.trash_origin_server_folder,
                                  delivery_state=None, delivery_error=None, **raw_fields)
            elif db.add_message(m):
                new_count += 1
            received.add(uid)
            if not full_history:
                # Не пропускаем нижний UID, если FETCH вернул неполный или переставленный набор.
                completed = set(known) | received
                prefix = [value for value in all_uids if value > last_uid]
                for value in prefix:
                    if value not in completed:
                        break
                    last_uid = value
        if set(batch) - received:
            raise imaplib.IMAP4.error(tr("сервер прислал не все письма, загрузка продолжится при следующей проверке"))
        if not full_history:
            last_uid = max(last_uid, max(batch))
        db.set_folder_state(acc.id, folder.name, validity, last_uid)
    if full_history:
        last_uid = max(all_uids, default=0)
    elif not new and all_uids:
        last_uid = max(last_uid, max(all_uids))
    db.set_folder_state(acc.id, folder.name, validity, last_uid)
    if full_history:
        with db.conn() as c:
            c.execute("UPDATE folders SET full_sync=1 WHERE account_id=? AND name=?", (acc.id, folder.name))
    return new_count, True


def synchronize(db, acc, progress=lambda text: None, full_history=False, cancel=None):
    # Импорт во время вызова исключает цикл между SMTP и синхронизацией.
    from .delivery import process_outbox
    lock = db.account_lock(acc.id)
    process_outbox(db, acc, cancel)  # каждое письмо блокирует ящик само
    if cancel and cancel.is_set():
        return 0
    im = wire.imap_connect(acc, wire.get_password(db, acc.id))
    total = 0
    errors = []
    try:
        folders = discover_folders(im)
        present = {folder.name for folder in folders}
        with lock, db.conn() as c:
            for old in db.server_folders(acc.id):
                if old["name"] not in present:
                    c.execute("UPDATE messages SET folder='archive',server_folder=NULL,uid=NULL,uidvalidity=NULL WHERE account_id=? AND server_folder=?", (acc.id, old["name"]))
                    c.execute("UPDATE folders SET view=NULL,full_sync=0 WHERE account_id=? AND name=?", (acc.id, old["name"]))
        for folder in folders:
            if cancel and cancel.is_set():
                return total
            with db.conn() as c:
                c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?) ON CONFLICT(account_id,name) DO UPDATE SET view=excluded.view,display_name=excluded.display_name",
                          (acc.id, folder.name, folder.view, folder.display))
            flags_done, preempted_count = False, 0
            try:
                while True:
                    # Ящик блокируется на одну папку; если пользователь ждёт (отправка,
                    # удаление, пометка), синхронизация уступает и продолжает после него.
                    with lock:
                        try:
                            count, complete = sync_mailbox(db, acc, im, folder, progress, full_history, cancel,
                                                           lock.yield_point, flags_done)
                            count += preempted_count
                            break
                        except SyncPreempted as preempted:
                            flags_done = flags_done or getattr(preempted, "flags_done", False)
                            preempted_count += getattr(preempted, "new_count", 0)
                    lock.wait_uncontended()
            except imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error as error:
                errors.append(folder.display + " — " + explain(error).summary.lower() + (f" ({server_text(error.args)})" if server_text(error.args) else ""))
                continue
            total += count
            if not complete:
                return total
        if errors:
            raise MailOperationError(tr("Не удалось синхронизировать папки: {0}", "; ".join(errors)))
        if full_history and not (cancel and cancel.is_set()):
            db.set_setting(f"history:{acc.id}", "complete")
        return total
    finally:
        try:
            im.logout()
        except Exception:
            pass
