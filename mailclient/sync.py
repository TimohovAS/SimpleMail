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


class ProgressText(str):
    """Текст прогресса для строки состояния с числами для полосы загрузки."""

    def __new__(cls, text, done=0, total=0):
        value = super().__new__(cls, text)
        value.done, value.total = done, total
        return value


def checked(result):
    typ, data = result
    if typ != "OK":
        raise imaplib.IMAP4.error(server_text(data) or tr("сервер не выполнил команду"))
    return data


def display_mailbox(name):
    return wire.decode_mailbox(name)


@dataclass
class ServerFolder:
    name: str
    view: str
    display: str
    flags: set


# Служебные папки, которые хостинги (cPanel, Dovecot) создают вместе с ящиком.
STANDARD_REMOTE = {"drafts": "Черновики на сервере", "draft": "Черновики на сервере",
                   "archive": "Архив", "archives": "Архив", "templates": "Шаблоны",
                   "junk": "Junk", "spam": "Junk", "junk e-mail": "Junk", "bulk mail": "Junk"}


def folder_base(name, delimiter="."):
    """Имя папки без служебного префикса: INBOX.Drafts → Drafts, [Gmail]/Черновики → Черновики."""
    for root in ("INBOX", "[Gmail]", "[Google Mail]"):
        for sep in {delimiter or ".", ".", "/"}:
            prefix = root + sep
            if name[:len(prefix)].casefold() == prefix.casefold() and len(name) > len(prefix):
                return name[len(prefix):]
    return name


# Gmail показывает одно письмо в нескольких «папках»-ярлыках. «Вся почта», «Важное» и «Помеченные»
# повторяют письма из других папок: их синхронизация удваивает загрузку и место на диске.
SKIPPED_FLAGS = {b"\\all", b"\\important", b"\\flagged"}


def standard_remote(name):
    """Служебная серверная папка (черновики, архив, второй Junk): в дереве видна, только если не пуста."""
    return folder_base(name).casefold() in STANDARD_REMOTE


def discover_folders(im):
    entries = []
    for item in checked(im.list('""', '"*"')) or []:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'^\(([^)]*)\)\s+("(?:\\.|[^"])*"|NIL)\s+(.+)$', line)
        if not match:
            continue
        flags, delimiter, name = match.groups()
        flags = {flag.lower() for flag in flags.split()}
        if b"\\noselect" in flags:
            continue
        delimiter = "" if delimiter == b"NIL" else re.sub(rb'\\(.)', rb'\1', delimiter[1:-1]).decode("utf-8", "replace")
        if isinstance(item, tuple):
            name = item[1]
        elif name.startswith(b'"') and name.endswith(b'"'):
            name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
        name = name.decode("utf-8")
        if wire.uses_utf8(im):
            name = wire.encode_mailbox(name)
        entries.append((name, flags, delimiter))
    def known_role(name, flags):
        lower = name.casefold()
        if lower == "inbox":
            return "inbox"
        if b"\\sent" in flags or lower in ("sent", "sent items", "sent messages", "inbox.sent"):
            return "sent"
        if b"\\trash" in flags or lower in ("trash", "deleted items", "deleted messages", "inbox.trash"):
            return "trash"
        if b"\\junk" in flags or lower in ("junk", "spam", "junk e-mail"):
            return "spam"
        return None

    views = {name: known_role(name, flags) for name, flags, _ in entries}
    # INBOX.Junk, INBOX.Sent у хостингов с префиксом INBOX.: роль по имени без префикса,
    # только если такой папки ещё нет — второй «Спам» не появляется.
    by_base = {"sent": "sent", "sent items": "sent", "sent messages": "sent", "trash": "trash",
               "deleted items": "trash", "deleted messages": "trash", "junk": "spam", "spam": "spam",
               "junk e-mail": "spam"}
    for name, flags, delimiter in entries:
        role = by_base.get(folder_base(name, delimiter).casefold())
        if views[name] is None and role and role not in views.values():
            views[name] = role
    result = []
    for name, flags, delimiter in entries:
        view = views[name]
        if view is None and flags & SKIPPED_FLAGS:
            result.append(ServerFolder(name, "skip", display_mailbox(name), flags))
        elif view is None:
            base = folder_base(name, delimiter)
            display = ("Черновики на сервере" if b"\\drafts" in flags else
                       STANDARD_REMOTE.get(base.casefold()) or display_mailbox(base))
            result.append(ServerFolder(name, "remote:" + name, display, flags))
        else:
            result.append(ServerFolder(name, view, display_mailbox(name), flags))
    if not any(folder.view == "inbox" for folder in result):
        result.insert(0, ServerFolder(INBOX, "inbox", INBOX, set()))
    return result


def _uid(meta):
    match = re.search(rb"\bUID (\d+)\b", meta)
    return int(match.group(1)) if match else None


class HistoryPaused(Exception):
    """Пользователь приостановил загрузку истории: папка досинхронизируется в обычном режиме."""

    def __init__(self, new_count=0, flags_done=False):
        super().__init__()
        self.new_count, self.flags_done = new_count, flags_done


def _response_int(im, code):
    try:
        _, data = im.response(code)
        return int(data[-1]) if data and data[-1] is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def supports_condstore(im) -> bool:
    caps = getattr(im, "capabilities", ())
    if not isinstance(caps, (tuple, list, set, frozenset)):
        return False
    return "CONDSTORE" in caps or "QRESYNC" in caps


def enable_condstore(im) -> bool:
    """Включить CONDSTORE на подключении: после этого SELECT/EXAMINE сообщает HIGHESTMODSEQ.

    ENABLE CONDSTORE (RFC 5161/7162) — штатный способ; серверам без ENABLE передаём
    параметр (CONDSTORE) в EXAMINE (imaplib сам его не умеет и заключил бы в кавычки)."""
    if not supports_condstore(im):
        return False
    if "ENABLE" in im.capabilities:
        try:
            typ, _ = im.enable("CONDSTORE")
            return typ == "OK"
        except Exception:
            return False
    im._simplemail_condstore_param = True
    return True


def select_folder(im, mailbox: bytes, condstore=False):
    if condstore and getattr(im, "_simplemail_condstore_param", False):
        im.untagged_responses = {}
        im.is_readonly = True
        typ, data = im._simple_command("EXAMINE", mailbox, "(CONDSTORE)")
        if typ != "OK":
            im.state = "AUTH"
            return typ, data
        im.state = "SELECTED"
        return typ, im.untagged_responses.get("EXISTS", [None])
    return im.select(mailbox, readonly=True)


def sync_mailbox(db, acc, im, folder, progress, full_history=False, cancel=None, yield_point=None, flags_done=False,
                 history_cancel=None, condstore=False, archived_spam=None):
    """Синхронизировать одну папку. yield_point() между шагами может бросить SyncPreempted —
    тогда папка синхронизируется заново, уже скачанные письма не скачиваются повторно.
    flags_done — состояния писем этой папки уже обновлены в этой проверке."""
    outer_yield = yield_point or (lambda: None)
    archived_spam = archived_spam if archived_spam is not None else set()
    state = {"flags_done": flags_done}

    def yield_point():
        try:
            outer_yield()
        except SyncPreempted as preempted:
            preempted.flags_done = state["flags_done"]
            preempted.new_count = new_count  # уже сохранённые письма этой попытки
            raise
    mailbox = imap_mailbox(folder.name, im)
    selected = checked(select_folder(im, mailbox, condstore))
    validity = _uidvalidity(im)
    if not validity:
        raise imaplib.IMAP4.error(tr("сервер не сообщил UIDVALIDITY папки, синхронизация невозможна"))
    marks = None
    since_modseq = None
    if condstore:
        modseq, uidnext = _response_int(im, "HIGHESTMODSEQ"), _response_int(im, "UIDNEXT")
        try:
            exists = int(selected[-1])
        except (TypeError, ValueError, IndexError):
            exists = None
        if modseq and uidnext and exists is not None:
            marks = (modseq, exists, uidnext)
            old_validity, old_modseq, old_exists, old_uidnext, complete_history = db.folder_marks(acc.id, folder.name)
            pending_local = db.conn().execute(
                "SELECT 1 FROM messages WHERE account_id=? AND server_folder=? AND "
                "(pending_seen IS NOT NULL OR pending_flagged IS NOT NULL) LIMIT 1", (acc.id, folder.name)).fetchone()
            if old_validity == validity and old_modseq:
                if (old_modseq, old_exists, old_uidnext) == marks and not pending_local \
                        and (not full_history or complete_history):
                    # Папка не менялась с прошлой проверки (RFC 7162): ни новых писем, ни
                    # удалений, ни изменённых флагов — дальше ничего не запрашиваем.
                    return 0, True
                since_modseq = old_modseq
    data = checked(im.uid("SEARCH", None, "ALL"))
    all_uids = sorted({int(token) for token in (data[0] or b"").split() if token.isdigit()})
    uid_set = set(all_uids)
    saved_validity, last_uid = db.folder_state(acc.id, folder.name)
    rows = db.conn().execute("SELECT id,uid,uidvalidity,pending_seen,pending_flagged,folder FROM messages WHERE account_id=? AND server_folder=?", (acc.id, folder.name)).fetchall()
    known, legacy = {}, {}
    for row in sorted(rows, key=lambda item: item["uidvalidity"] != validity):
        same_epoch = row["uidvalidity"] in (None, validity)
        if same_epoch and row["uid"] in uid_set and row["uid"] not in known:
            known[row["uid"]] = row["id"]
            if row["uidvalidity"] is None:
                legacy[row["uid"]] = row["id"]
        else:
            if row["folder"] == "spam":
                archived_spam.add(row["id"])
            db.update_message(row["id"], server_folder=None, uid=None, uidvalidity=None, folder="archive")
    if saved_validity != validity:
        last_uid = 0
    new = [uid for uid in all_uids if uid not in known]
    if not full_history:
        limit = max(1, int(db.get_setting("initial_fetch_limit", "300")))
        # Первая проверка: сначала свежие письма, чтобы почтой можно было пользоваться сразу.
        fetch = [uid for uid in new if uid > last_uid] if last_uid else new[-limit:]
        if db.get_setting(f"history:{acc.id}") is None:
            # Сколько старых писем не скачано — окно предложит скачать их (и ящикам,
            # добавленным до появления этого вопроса).
            db.set_setting(f"history_skipped:{acc.id}:{folder.name}", len(set(new) - set(fetch)))
        new = fetch
    # Не присваиваем старой записи поколение папки без проверки её содержимого.
    new = sorted(set(new) | set(legacy))
    new_count = 0
    pending = [row for row in rows if row["uid"] in known and row["uid"] not in legacy
               and (row["pending_seen"] is not None or row["pending_flagged"] is not None)]
    if pending:
        yield_point()
        checked(im.select(imap_mailbox(folder.name, im)))
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
        if since_modseq:
            # Только письма, у которых флаги изменились после прошлой проверки.
            fetched_flags = checked(im.uid("FETCH", "1:*", f"(UID FLAGS) (CHANGEDSINCE {since_modseq})"))
        else:
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
    started = time.monotonic()
    for offset in range(0, len(new), FETCH_BATCH):
        if cancel and cancel.is_set():
            return new_count, False
        if full_history and history_cancel and history_cancel.is_set():
            # Новые письма (UID больше last_uid) ещё не скачаны: их заберёт обычная синхронизация.
            raise HistoryPaused(new_count, state["flags_done"])
        yield_point()
        batch = new[offset:offset + FETCH_BATCH]
        done = offset + len(batch)
        text = tr("{0} / {1}: загрузка писем {2} из {3}", acc.name, folder.display, done, len(new))
        if offset and len(new) - offset > FETCH_BATCH:
            left = (time.monotonic() - started) / offset * (len(new) - offset)
            text += " · " + (tr("осталось ~{0} мин", round(left / 60)) if left >= 90 else tr("осталось ~{0} с", max(5, round(left, -1))))
        progress(ProgressText(text, done, len(new)))
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
            returned_from_spam = folder.view == "inbox" and _returned_from_spam(db, m, archived_spam)
            if returned_from_spam:
                m.is_spam, m.spam_score, m.spam_reason = False, 0.0, tr("перемещено из спама на сервере")
            elif folder.view == "inbox":
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
                if returned_from_spam:
                    raw_fields.update(is_spam=0, spam_score=0.0, spam_reason=m.spam_reason)
                if candidate.raw != m.raw:
                    if acc.storage_dir:
                        path = storage.eml_path(acc.storage_dir, m.folder, m.date_ts, uid, m.raw, f"{folder.name}:{validity}")
                        storage.write_eml(path, m.raw)
                        raw_fields.update(raw=None, raw_path=str(path), size=len(m.raw))
                    else:
                        raw_fields.update(raw=m.raw, raw_path=None, size=len(m.raw))
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
    db.set_folder_marks(acc.id, folder.name, *(marks or (None, None, None)))
    if full_history:
        with db.conn() as c:
            c.execute("UPDATE folders SET full_sync=1 WHERE account_id=? AND name=?", (acc.id, folder.name))
    return new_count, True


def _returned_from_spam(db, message, archived_spam):
    sql = "SELECT id,folder FROM messages WHERE account_id=? AND "
    if message.message_id:
        sql += "message_id=?"
        params = (message.account_id, message.message_id)
    else:
        sql += "subject=? AND sender=? AND body_text=?"
        params = (message.account_id, message.subject, message.sender, message.body_text)
    for row in db.conn().execute(sql, params).fetchall():
        if row["folder"] != "spam" and row["id"] not in archived_spam:
            continue
        old = db.message(row["id"])
        if old and old.raw and (old.raw == message.raw or _same_content(old.raw, message.raw)):
            return True
    return False


def synchronize(db, acc, progress=lambda text: None, full_history=False, cancel=None, history_cancel=None,
                inbox_only=False):
    """inbox_only — проверка по сигналу IDLE: изменилась только папка «Входящие»."""
    # Импорт во время вызова исключает цикл между SMTP и синхронизацией.
    from .delivery import process_outbox
    lock = db.account_lock(acc.id)
    process_outbox(db, acc, cancel)  # каждое письмо блокирует ящик само
    if cancel and cancel.is_set():
        return 0
    im = wire.imap_connect(acc, wire.get_password(db, acc.id, account=acc))
    total = 0
    errors = []
    archived_spam = set()
    try:
        folders = discover_folders(im)
        junk_destination = next((folder.name for folder in folders if folder.view == "spam"), None)
        present = {folder.name for folder in folders}
        skipped = [folder for folder in folders if folder.view == "skip"]
        folders = [folder for folder in folders if folder.view != "skip"]
        for folder in skipped:
            # Копии писем из «Вся почта»/«Важное», скачанные прежними версиями: оригиналы остаются
            # в своих папках и на сервере.
            with lock:
                for row in db.conn().execute("SELECT id FROM messages WHERE account_id=? AND server_folder=?",
                                             (acc.id, folder.name)).fetchall():
                    db.delete_message(row["id"])
                with db.conn() as c:
                    c.execute("UPDATE folders SET view=NULL,full_sync=0 WHERE account_id=? AND name=?", (acc.id, folder.name))
                    c.execute("DELETE FROM settings WHERE key=?", (f"history_skipped:{acc.id}:{folder.name}",))
        with lock, db.conn() as c:
            for old in db.server_folders(acc.id):
                if old["name"] not in present:
                    c.execute("UPDATE messages SET folder='archive',server_folder=NULL,uid=NULL,uidvalidity=NULL WHERE account_id=? AND server_folder=?", (acc.id, old["name"]))
                    c.execute("UPDATE folders SET view=NULL,full_sync=0 WHERE account_id=? AND name=?", (acc.id, old["name"]))
        if inbox_only:
            folders = [folder for folder in folders if folder.view == "inbox"] or folders
        condstore = enable_condstore(im)
        for folder in folders:
            if cancel and cancel.is_set():
                return total
            with db.conn() as c:
                c.execute("INSERT INTO folders(account_id,name,view,display_name) VALUES(?,?,?,?) ON CONFLICT(account_id,name) DO UPDATE SET view=excluded.view,display_name=excluded.display_name",
                          (acc.id, folder.name, folder.view, folder.display))
            flags_done, preempted_count = False, 0
            folder_history = full_history
            try:
                while True:
                    if history_cancel and history_cancel.is_set():
                        folder_history = False
                    # Ящик блокируется на одну папку; если пользователь ждёт (отправка,
                    # удаление, пометка), синхронизация уступает и продолжает после него.
                    with lock:
                        try:
                            count, complete = sync_mailbox(db, acc, im, folder, progress, folder_history, cancel,
                                                           lock.yield_point, flags_done, history_cancel, condstore, archived_spam)
                            count += preempted_count
                            break
                        except HistoryPaused as paused:
                            flags_done = flags_done or paused.flags_done
                            preempted_count += paused.new_count
                            continue
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
        junk = junk_destination
        spam_rows = db.conn().execute("SELECT id FROM messages WHERE account_id=? AND folder='inbox' AND is_spam=1 "
                                      "AND server_folder IS NOT NULL", (acc.id,)).fetchall() if junk else []
        if spam_rows:
            # Без MOVE/UIDPLUS безопасного переноса нет: автоспам остаётся помеченным локально,
            # без ошибки при каждой проверке.
            caps = {token.upper() for line in checked(im.capability()) for token in (line or b"").split()}
            if b"MOVE" not in caps and b"UIDPLUS" not in caps:
                spam_rows = []
        if spam_rows:
            from .mailops import _server_move
            for row in spam_rows:
                if cancel and cancel.is_set():
                    return total
                try:
                    with lock:
                        message = db.message(row["id"])
                        if message and message.folder == "inbox" and message.is_spam:
                            fields = _server_move(db, message, junk, connection=im)
                            db.update_message(message.id, folder="spam", **fields)
                    lock.wait_uncontended()
                except Exception as error:
                    summary = explain(error).summary
                    if summary not in errors:
                        errors.append(summary)
            if errors:
                raise MailOperationError(tr("Не удалось переместить автоспам на сервере: {0}", "; ".join(errors)))
        if full_history and not (cancel and cancel.is_set()) and not (history_cancel and history_cancel.is_set()):
            db.set_setting(f"history:{acc.id}", "complete")
        return total
    finally:
        try:
            im.logout()
        except Exception:
            pass
