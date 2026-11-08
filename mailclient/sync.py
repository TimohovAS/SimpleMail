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
from .protocols import FETCH_BATCH, INBOX, _uidvalidity, get_password, imap_connect, imap_mailbox, store_flag, store_mark_flags
from .spam import SpamFilter
from . import storage
from .delivery import _same_content
from .errors import MailOperationError, explain, server_text
from .db import MARK_FLAGS, SyncPreempted

HEADER_BATCH = 100
HEADER_REFRESH_SECONDS = 60


class ProgressText(str):
    """Текст прогресса для строки состояния с числами для полосы загрузки."""

    def __new__(cls, text, done=0, total=0, messages_changed=False, *,
                account_id=None, message_ids=(), change_kind=None):
        value = super().__new__(cls, text)
        value.done, value.total = done, total
        value.messages_changed = messages_changed
        value.account_id = account_id
        value.message_ids = tuple(message_ids)
        value.change_kind = change_kind
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


def server_marks(flags) -> int:
    """Отметки MARK_* по флагам письма на сервере (флаги IMAP не зависят от регистра)."""
    flags = {flag.lower() for flag in flags}
    return sum(mark for mark, flag in MARK_FLAGS.items() if flag.lower().encode() in flags)


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
                 history_cancel=None, condstore=False, archived_spam=None, headers_only=False):
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
                "(pending_seen IS NOT NULL OR pending_flagged IS NOT NULL OR pending_marks!=0) LIMIT 1", (acc.id, folder.name)).fetchone()
            pending_bodies = not headers_only and db.conn().execute(
                "SELECT 1 FROM messages WHERE account_id=? AND server_folder=? AND body_downloaded=0 LIMIT 1",
                (acc.id, folder.name)).fetchone()
            if old_validity == validity and old_modseq:
                if (old_modseq, old_exists, old_uidnext) == marks and not pending_local and not pending_bodies \
                        and (not full_history or complete_history):
                    # Папка не менялась с прошлой проверки (RFC 7162): ни новых писем, ни
                    # удалений, ни изменённых флагов — дальше ничего не запрашиваем.
                    return 0, True
                since_modseq = old_modseq
    data = checked(im.uid("SEARCH", None, "ALL"))
    all_uids = sorted({int(token) for token in (data[0] or b"").split() if token.isdigit()})
    uid_set = set(all_uids)
    saved_validity, last_uid = db.folder_state(acc.id, folder.name)
    recent_limit = max(1, int(db.get_setting("initial_fetch_limit", "300")))
    floor_key = f"body_recent_floor:{acc.id}:{folder.name}"
    saved_floor = db.get_setting(floor_key, "") or ""
    if not saved_floor.startswith(f"{validity}:") or (saved_floor == f"{validity}:0" and all_uids):
        # Keep the ordinary recent-mail window separately from the history
        # watermark: pausing history must not restart old bodies on the next F5.
        recent = all_uids[-recent_limit:]
        db.set_setting(floor_key, f"{validity}:{min(recent, default=0)}")
    db.set_server_count(acc.id, folder.name, len(all_uids))  # для «скачано X из Y» в меню ящика
    rows = db.conn().execute("SELECT id,uid,uidvalidity,pending_seen,pending_flagged,pending_marks,folder,body_downloaded FROM messages WHERE account_id=? AND server_folder=?", (acc.id, folder.name)).fetchall()
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
        limit = recent_limit
        # Первая проверка: сначала свежие письма, чтобы почтой можно было пользоваться сразу.
        fetch = [uid for uid in new if uid > last_uid] if last_uid else [uid for uid in all_uids[-limit:] if uid in new]
        if db.get_setting(f"history:{acc.id}") is None:
            # Сколько старых писем не скачано — окно предложит скачать их (и ящикам,
            # добавленным до появления этого вопроса).
            db.set_setting(f"history_skipped:{acc.id}:{folder.name}", len(set(new) - set(fetch)))
        new = fetch
    # Не присваиваем старой записи поколение папки без проверки её содержимого.
    unfinished = {row["uid"]: row["id"] for row in rows if row["uid"] in known
                  and row["uidvalidity"] == validity and not row["body_downloaded"]}
    new = sorted(set(new) | set(legacy) | (set() if headers_only else set(unfinished)), reverse=headers_only)
    requested = sorted(new)
    original_last_uid = last_uid
    completed_uids = set(known) - set(legacy)
    new_count = 0
    pending = [row for row in rows if row["uid"] in known and row["uid"] not in legacy
               and (row["pending_seen"] is not None or row["pending_flagged"] is not None or row["pending_marks"])]
    if pending:
        yield_point()
        checked(im.select(imap_mailbox(folder.name, im)))
        if _uidvalidity(im) != validity:
            raise imaplib.IMAP4.error(tr("папка на сервере изменилась во время проверки, она будет проверена в следующий раз"))
        # Пометки, сделанные без связи с сервером: «Прочитано» и флажок «Важное» — пачками UID
        # (одна команда на сотни писем, а не на каждое).
        for column, flag in (("pending_seen", "\\Seen"), ("pending_flagged", "\\Flagged")):
            for value in (1, 0):
                rows_with = [row for row in pending if row[column] == value]
                if not rows_with:
                    continue
                store_flag(im, [row["uid"] for row in rows_with], flag, bool(value))
                with db.conn() as c:
                    c.executemany(f"UPDATE messages SET {column}=NULL WHERE id=? AND {column}=?",
                                  [(row["id"], value) for row in rows_with])
        for row in pending:
            # «Ответили»/«переслали»: письмо отправлено, когда не было связи с сервером.
            if row["pending_marks"]:
                store_mark_flags(im, row["uid"], row["pending_marks"])
                with db.conn() as c:
                    c.execute("UPDATE messages SET pending_marks=pending_marks&~? WHERE id=?", (row["pending_marks"], row["id"]))
    # FLAGS обновляем и у писем, прочитанных или изменённых другим клиентом.
    # One request, but avoid scanning years of mail when only a recent window is
    # stored locally. For a large local history, 1:* avoids giant command lines.
    known_uids = sorted(uid for uid in known if uid not in legacy)
    if known_uids and not flags_done:
        if cancel and cancel.is_set():
            return new_count, False
        yield_point()
        progress(tr("{0} / {1}: обновление состояний {2} из {3}", acc.name, folder.display, len(known_uids), len(known_uids)))
        flag_target = ",".join(map(str, known_uids)) if len(known_uids) <= 500 else "1:*"
        if since_modseq:
            # Только письма, у которых флаги изменились после прошлой проверки.
            fetched_flags = checked(im.uid("FETCH", flag_target, f"(UID FLAGS) (CHANGEDSINCE {since_modseq})"))
        else:
            fetched_flags = checked(im.uid("FETCH", flag_target, "(UID FLAGS)"))
        with db.conn() as c:
            for item in fetched_flags:
                meta = item[0] if isinstance(item, tuple) else item
                if isinstance(meta, bytes) and (uid := _uid(meta)) in known and uid not in legacy:
                    flags = imaplib.ParseFlags(meta)
                    # Отметки только добавляются: $Forwarded хранят не все серверы, а снять
                    # «ответили» другие почтовые программы не дают.
                    values = (int(b"\\Seen" in flags), int(b"\\Flagged" in flags), server_marks(flags), validity,
                              "archive" if b"\\Deleted" in flags else folder.view)
                    # Unchanged FLAGS must not rewrite the row: the existing FTS
                    # update trigger would otherwise reindex every full body.
                    c.execute("UPDATE messages SET seen=coalesce(pending_seen,?),flagged=coalesce(pending_flagged,?),"
                              "marks=marks|?,uidvalidity=?,folder=? WHERE id=? AND ("
                              "seen IS NOT coalesce(pending_seen,?) OR flagged IS NOT coalesce(pending_flagged,?) OR "
                              "marks!=(marks|?) OR uidvalidity IS NOT ? OR folder IS NOT ?)",
                              (*values, known[uid], *values))
        state["flags_done"] = True
    started = time.monotonic()
    batches = ([new[i:i + HEADER_BATCH] for i in range(0, len(new), HEADER_BATCH)]
               if headers_only else _size_batches(im, new))
    offsets = [sum(len(item) for item in batches[:index]) for index in range(len(batches))]
    for offset, batch in zip(offsets, batches):
        if cancel and cancel.is_set():
            return new_count, False
        if full_history and history_cancel and history_cancel.is_set():
            # Новые письма (UID больше last_uid) ещё не скачаны: их заберёт обычная синхронизация.
            raise HistoryPaused(new_count, state["flags_done"])
        yield_point()
        done = offset + len(batch)
        text = (tr("{0} / {1}: загрузка заголовков {2} из {3}", acc.name, folder.display, done, len(new))
                if headers_only else tr("{0} / {1}: загрузка писем {2} из {3}", acc.name, folder.display, done, len(new)))
        if offset and len(new) - offset > FETCH_BATCH:
            left = (time.monotonic() - started) / offset * (len(new) - offset)
            text += " · " + (tr("осталось ~{0} мин", round(left / 60)) if left >= 90 else tr("осталось ~{0} с", max(5, round(left, -1))))
        progress(ProgressText(text, done, len(new)))
        query = "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER])" if headers_only else "(UID FLAGS INTERNALDATE BODY.PEEK[])"
        fetched = checked(im.uid("FETCH", ",".join(map(str, batch)), query))
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
            header_stub = headers_only
            if header_stub and (uid in legacy or (m.message_id and db.conn().execute(
                    "SELECT 1 FROM messages WHERE account_id=? AND message_id=? AND server_folder IS NULL "
                    "AND body_downloaded=1 AND folder NOT IN ('drafts','outbox') LIMIT 1",
                    (acc.id, m.message_id)).fetchone())):
                # Reattaching a moved/legacy message needs full content comparison.
                # This exceptional verification preserves the already-existing ID.
                yield_point()
                full = checked(im.uid("FETCH", str(uid), "(UID FLAGS INTERNALDATE BODY.PEEK[])"))
                item = next((part for part in full if isinstance(part, tuple) and isinstance(part[0], bytes)
                             and _uid(part[0]) == uid), None)
                if item is None:
                    continue
                m, header_stub = parse_message(item[1]), False
            if not m.date_ts:
                # Нет заголовка Date (или он испорчен): берём время получения письма сервером.
                arrived = imaplib.Internaldate2tuple(item[0])
                if arrived:
                    m.date_ts = int(time.mktime(arrived))
            m.account_id, m.folder, m.server_folder, m.uid, m.uidvalidity = acc.id, folder.view, folder.name, uid, validity
            m.seen = b"\\Seen" in imaplib.ParseFlags(item[0])
            m.flagged = b"\\Flagged" in imaplib.ParseFlags(item[0])
            m.marks = server_marks(imaplib.ParseFlags(item[0]))
            if b"\\Deleted" in imaplib.ParseFlags(item[0]):
                m.folder = "archive"
            if header_stub:
                size = re.search(rb"RFC822\.SIZE (\d+)", item[0])
                m.size = int(size.group(1)) if size else None
                m.raw = b""
                m.body_text = m.body_html = ""
                m.body_downloaded = False
            returned_from_spam = not header_stub and folder.view == "inbox" and _returned_from_spam(db, m, archived_spam)
            if returned_from_spam:
                m.is_spam, m.spam_score, m.spam_reason = False, 0.0, tr("перемещено из спама на сервере")
            elif not header_stub and folder.view == "inbox":
                verdict = SpamFilter(db).classify(m)
                m.is_spam, m.spam_score, m.spam_reason = verdict.is_spam, verdict.score, verdict.reason
            if folder.view == "trash":
                m.trash_origin_folder, m.trash_origin_server_folder = "inbox", INBOX
            if not header_stub and uid in unfinished:
                db.complete_message_body(unfinished[uid], m)
                received.add(uid)
                completed_uids.add(uid)
                continue
            # При перемещении другим клиентом сохраняем прежний локальный ID.
            candidate = None
            if uid in legacy:
                old = db.message(legacy[uid])
                if old and (old.raw == m.raw or _same_content(old.raw, m.raw)):
                    candidate = old
                elif old:
                    db.update_message(old.id, server_folder=None, uid=None, uidvalidity=None, folder="archive")
            if not header_stub and m.message_id:
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
                                  marks=candidate.marks | m.marks,
                                  trash_origin_folder=candidate.trash_origin_folder or m.trash_origin_folder,
                                  trash_origin_server_folder=candidate.trash_origin_server_folder or m.trash_origin_server_folder,
                                  delivery_state=None, delivery_error=None, **raw_fields)
            elif db.add_message(m):
                new_count += 1
            received.add(uid)
            completed_uids.add(uid)
            if not full_history:
                # Не пропускаем нижний UID, если FETCH вернул неполный или переставленный набор.
                completed = set(known) | received
                prefix = [value for value in all_uids if value > last_uid]
                for value in prefix:
                    if value not in completed:
                        break
                    last_uid = value
        if headers_only:
            last_uid = original_last_uid
            for value in requested:
                if value not in completed_uids:
                    break
                last_uid = max(last_uid, value)
            db.set_folder_state(acc.id, folder.name, validity, last_uid)
        # Commit notifications happen after rows exist, including a partial FETCH.
        progress(ProgressText(text, len(completed_uids.intersection(requested)), len(new),
                              messages_changed=True, account_id=acc.id, change_kind="headers"))
        if set(batch) - received:
            raise imaplib.IMAP4.error(tr("сервер прислал не все письма, загрузка продолжится при следующей проверке"))
        if not full_history and not headers_only:
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


BATCH_BYTES = 2 * 1024 * 1024
GMAIL_BODY_BATCH = 50


def _size_batches(im, uids):
    """Пачки не больше FETCH_BATCH писем и ~2 МБ: письмо с большим вложением идёт отдельно.

    Антивирус, проверяющий соединение (Kaspersky для Gmail), отдаёт пачку только после проверки
    всех вложений в ней: крупные пачки давали паузы дольше таймаута."""
    plain = [uids[i:i + FETCH_BATCH] for i in range(0, len(uids), FETCH_BATCH)]
    if len(uids) < 2:
        return plain
    sizes = {}
    try:
        for chunk in range(0, len(uids), 500):
            typ, data = im.uid("FETCH", ",".join(map(str, uids[chunk:chunk + 500])), "(UID RFC822.SIZE)")
            if typ != "OK":
                return plain
            for item in data or []:
                meta = item[0] if isinstance(item, tuple) else item
                if isinstance(meta, bytes) and (uid := _uid(meta)) is not None:
                    match = re.search(rb"RFC822\.SIZE (\d+)", meta)
                    if match:
                        sizes[uid] = int(match.group(1))
    except (imaplib.IMAP4.error, AttributeError, TypeError, ValueError):
        return plain
    batches, current, total = [], [], 0
    for uid in uids:
        size = sizes.get(uid, 64 * 1024)
        if current and (len(current) >= FETCH_BATCH or total + size > BATCH_BYTES):
            batches.append(current)
            current, total = [], 0
        current.append(uid)
        total += size
    if current:
        batches.append(current)
    return batches


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


def _archive_unavailable(db, message):
    """Keep visible metadata, but never attach it to a recycled server UID."""
    db.update_message(message.id, server_folder=None, uid=None, uidvalidity=None, folder="archive")


def _complete_body_response(db, acc, im, message, validity, item, cancel=None, archived_spam=None):
    """Validate one UID payload and fill its stub while holding the account lock."""
    if cancel and cancel.is_set():
        return False
    if message.uidvalidity != validity:
        _archive_unavailable(db, message)
        raise MailOperationError(tr("Папка на сервере изменилась. Заголовок сохранён в архиве; обновите список писем."))
    if item is None:
        # An incomplete FETCH is retryable; a deleted UID must never turn into
        # an apparently complete empty message or be retried forever.
        found = checked(im.uid("SEARCH", None, f"UID {message.uid}"))
        uids = {int(value) for value in (found[0] or b"").split() if value.isdigit()}
        if message.uid not in uids:
            _archive_unavailable(db, message)
            raise MailOperationError(tr("Письмо удалено с сервера до загрузки содержимого. Его заголовок сохранён в архиве."))
        raise imaplib.IMAP4.error(tr("сервер прислал не все письма, загрузка продолжится при следующей проверке"))
    meta, raw = item
    literal = re.search(rb"\{(\d+)\}\s*$", meta)
    advertised = re.search(rb"RFC822\.SIZE (\d+)", meta)
    received_size = len(raw)
    literal_size = int(literal.group(1)) if literal else None
    expected_size = int(advertised.group(1)) if advertised else message.size
    if not raw:
        raise imaplib.IMAP4.error(tr("Сервер прислал пустое содержимое письма"))
    if literal_size is not None and received_size != literal_size:
        raise imaplib.IMAP4.error(tr("Получено {0} байт, в блоке IMAP объявлено {1}", received_size, literal_size))
    # Gmail can underreport RFC822.SIZE even in the same response as BODY[].
    # Accept a larger, fully framed literal while retaining the short-body guard.
    if expected_size is not None and (received_size < expected_size or
                                      (literal_size is None and received_size != expected_size)):
        raise imaplib.IMAP4.error(tr("Получено {0} байт письма, ожидаемый размер — {1}", received_size, expected_size))
    m = parse_message(raw)
    m.account_id, m.server_folder, m.uidvalidity, m.uid = acc.id, message.server_folder, validity, message.uid
    m.folder = message.folder
    m.date_ts = m.date_ts or message.date_ts
    if m.folder == "inbox":
        if _returned_from_spam(db, m, archived_spam or set()):
            m.is_spam, m.spam_score, m.spam_reason = False, 0.0, tr("перемещено из спама на сервере")
        else:
            verdict = SpamFilter(db).classify(m)
            m.is_spam, m.spam_score, m.spam_reason = verdict.is_spam, verdict.score, verdict.reason
    if cancel and cancel.is_set():
        return False
    return db.complete_message_body(message.id, m)


def _body_responses(data):
    """Unsolicited FLAGS and another UID's body must never fill the requested row."""
    responses = {}
    for item in data:
        if (isinstance(item, tuple) and isinstance(item[0], bytes) and isinstance(item[1], bytes)
                and (uid := _uid(item[0])) is not None):
            responses.setdefault(uid, item)
    return responses


def _download_body(db, acc, im, message, validity, cancel=None, archived_spam=None):
    """One foreground BODY.PEEK request; reuse fetched bytes instead of rereading .eml."""
    if cancel and cancel.is_set():
        return None
    if message.uidvalidity != validity:
        _archive_unavailable(db, message)
        raise MailOperationError(tr("Папка на сервере изменилась. Заголовок сохранён в архиве; обновите список писем."))
    data = checked(im.uid("FETCH", str(message.uid), "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"))
    item = _body_responses(data).get(message.uid)
    if not _complete_body_response(db, acc, im, message, validity, item, cancel, archived_spam):
        return None
    loaded = db.message(message.id, with_raw=False)
    if loaded is not None:
        loaded.raw = item[1]
    return loaded


def fetch_message_body(db, message_id, cancel=None):
    """Download an opened message ahead of background sync, preserving its row ID.

    Returns the complete Message, or None after cancellation/deletion. Expected
    failures are explained through MailOperationError; no message is marked seen.
    """
    message = db.message(message_id, with_raw=False)
    if message is None or (cancel and cancel.is_set()):
        return None
    if message.body_downloaded:
        return db.message(message_id)
    with db.account_lock(message.account_id):
        message = db.message(message_id, with_raw=False)
        if message is None or (cancel and cancel.is_set()):
            return None
        if message.body_downloaded:
            return db.message(message_id)
        if not message.server_folder or message.uid is None or message.uidvalidity is None:
            raise MailOperationError(tr("Содержимое письма ещё не загружено, а копия на сервере недоступна."))
        acc = db.account(message.account_id)
        if acc is None:
            return None
        im = wire.imap_connect(acc, wire.get_password(db, acc.id, account=acc))
        try:
            checked(select_folder(im, imap_mailbox(message.server_folder, im)))
            validity = _uidvalidity(im)
            if not validity:
                raise imaplib.IMAP4.error(tr("сервер не сообщил UIDVALIDITY папки, синхронизация невозможна"))
            return _download_body(db, acc, im, message, validity, cancel)
        finally:
            try:
                im.logout()
            except Exception:
                pass


def _pending_body_batch(rows, limit=FETCH_BATCH):
    """Newest folder first, bounded by known header sizes; unknown/large bodies alone.

    Gathering nearby messages of the same folder avoids an EXAMINE round trip for
    every alternating inbox/sent message, while limiting foreground waiting time.
    """
    batch, remaining, total = [], [], 0
    folder = rows[0]["server_folder"]
    full = False
    for row in rows:
        size = row["size"] if row["size"] and row["size"] > 0 else BATCH_BYTES
        if row["server_folder"] != folder or full:
            remaining.append(row)
        elif batch and (len(batch) >= limit or total + size > BATCH_BYTES):
            # Do not jump over a large newer message to fill this batch with old mail.
            full = True
            remaining.append(row)
        else:
            batch.append(row)
            total += size
    return batch, remaining


def _download_error(folder, operation, error):
    reason = server_text(error.args) or explain(error).summary
    return f"{folder} — {operation}: {reason}"


def _download_pending_bodies(db, acc, im, folders, progress, cancel, lock, archived_spam,
                             full_history=False, history_cancel=None, condstore=False):
    """Fetch bounded batches, yielding the account to foreground actions between them."""
    allowed = {folder.name: folder.display for folder in folders}
    # Gmail adds substantial per-command latency; collect more small payloads
    # while retaining the same byte bound and foreground yield between batches.
    batch_limit = (GMAIL_BODY_BATCH if acc.imap_host.casefold().rstrip(".") in
                   {"imap.gmail.com", "imap.googlemail.com"} else FETCH_BATCH)
    rows = db.conn().execute("SELECT id,server_folder,uid,uidvalidity,size FROM messages WHERE account_id=? AND body_downloaded=0 "
                             "AND server_folder IS NOT NULL ORDER BY date_ts DESC,uid DESC", (acc.id,)).fetchall()
    rows = [row for row in rows if row["server_folder"] in allowed]
    recent_only = not full_history and db.get_setting(f"history:{acc.id}") in {"paused", "declined"}
    if recent_only:
        def ordinary(row):
            value = db.get_setting(f"body_recent_floor:{acc.id}:{row['server_folder']}", "") or ""
            try:
                epoch, floor = map(int, value.split(":"))
                return row["uidvalidity"] == epoch and row["uid"] >= floor
            except (ValueError, TypeError):
                return False
        rows = [row for row in rows if ordinary(row)]
    errors = []
    new_count = 0
    last_header_check = time.monotonic()
    inbox = next((folder for folder in folders if folder.view == "inbox"), None)
    selected, validity = None, None
    queued = {row["id"] for row in rows}
    done, total = 0, len(rows)

    def stopped():
        return (cancel and cancel.is_set()) or (full_history and history_cancel and history_cancel.is_set())

    while rows:
        if stopped():
            return errors, new_count
        # Never queue behind background work before allowing an already waiting
        # foreground action to acquire its priority account lock.
        lock.wait_uncontended()
        with lock:
            if inbox and time.monotonic() - last_header_check >= HEADER_REFRESH_SECONDS:
                # Large histories can take hours. New inbox headers must appear
                # while the older bodies continue downloading in the background.
                selected = None
                try:
                    count, _ = sync_mailbox(db, acc, im, inbox, progress, cancel=cancel,
                                             yield_point=lock.yield_point, headers_only=True,
                                             archived_spam=archived_spam, condstore=condstore)
                    new_count += count
                except SyncPreempted as interrupted:
                    new_count += getattr(interrupted, "new_count", 0)
                    continue
                except imaplib.IMAP4.abort:
                    raise
                except imaplib.IMAP4.error as error:
                    errors.append(_download_error(inbox.display, tr("Обновление заголовков"), error))
                last_header_check = time.monotonic()
                fresh = db.conn().execute("SELECT id,server_folder,uid,uidvalidity,size FROM messages WHERE account_id=? "
                                          "AND server_folder=? AND body_downloaded=0 ORDER BY uid DESC",
                                          (acc.id, inbox.name)).fetchall()
                fresh = [item for item in fresh if item["id"] not in queued
                         and (not recent_only or ordinary(item))]
                queued.update(item["id"] for item in fresh)
                rows = fresh + rows
                total += len(fresh)
            batch, rows = _pending_body_batch(rows, batch_limit)
            messages = []
            for row in batch:
                message = db.message(row["id"], with_raw=False)
                if (message is None or message.body_downloaded or message.server_folder not in allowed
                        or message.server_folder != row["server_folder"]):
                    done += 1
                    continue
                messages.append(message)
            if not messages or stopped():
                continue
            unprocessed = len(messages)
            folder = messages[0].server_folder
            operation = tr("Открытие папки")
            try:
                if selected != folder:
                    checked(select_folder(im, imap_mailbox(folder, im), condstore))
                    validity = _uidvalidity(im)
                    if not validity:
                        raise imaplib.IMAP4.error(tr("сервер не сообщил UIDVALIDITY папки, синхронизация невозможна"))
                    selected = folder
                valid = []
                for message in messages:
                    if message.uidvalidity != validity:
                        _archive_unavailable(db, message)
                        reason = tr("Папка на сервере изменилась. Заголовок сохранён в архиве; обновите список писем.")
                        errors.append(f"{allowed[folder]} — UID {message.uid}: {reason}")
                        done += 1
                        unprocessed -= 1
                    else:
                        valid.append(message)
                if not valid:
                    continue
                text = tr("{0}: загрузка содержимого писем {1} из {2}", acc.name, done + 1, total)
                progress(ProgressText(text, done, total))
                uids = ",".join(str(message.uid) for message in valid)
                operation = f"UID FETCH {uids}"
                data = checked(im.uid("FETCH", uids,
                                      "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[])"))
                responses = _body_responses(data)
                # Recheck cancellation between payloads: a history pause must not
                # save the remainder of an already received batch behind the user's back.
                for message in valid:
                    if stopped():
                        return errors, new_count
                    try:
                        loaded = _complete_body_response(db, acc, im, message, validity,
                                                         responses.get(message.uid), cancel, archived_spam)
                        done += 1
                        unprocessed -= 1
                        if loaded:
                            progress(ProgressText(tr("{0}: загрузка содержимого писем {1} из {2}", acc.name, done, total),
                                                  done, total, messages_changed=True,
                                                  account_id=acc.id, message_ids=(message.id,), change_kind="body"))
                    except imaplib.IMAP4.abort:
                        raise
                    except (imaplib.IMAP4.error, MailOperationError) as error:
                        done += 1
                        unprocessed -= 1
                        errors.append(_download_error(allowed[folder], f"UID {message.uid}", error))
                        progress(ProgressText(str(error), done, total, messages_changed=True,
                                              account_id=acc.id, message_ids=(message.id,), change_kind="body"))
            except imaplib.IMAP4.abort:
                raise
            except (imaplib.IMAP4.error, MailOperationError) as error:
                errors.append(_download_error(allowed[folder], operation, error))
                done += unprocessed
                progress(ProgressText(str(error), done, total, messages_changed=True,
                                      account_id=acc.id, message_ids=tuple(message.id for message in messages),
                                      change_kind="body"))
    return errors, new_count


def synchronize(db, acc, progress=lambda text: None, full_history=False, cancel=None, history_cancel=None,
                inbox_only=False):
    """inbox_only — проверка по сигналу IDLE: изменилась только папка «Входящие»."""
    # Импорт во время вызова исключает цикл между SMTP и синхронизацией.
    from .delivery import process_outbox
    lock = db.account_lock(acc.id)
    progress(tr("{0}: подключение к почте…", acc.name))
    process_outbox(db, acc, cancel)  # каждое письмо блокирует ящик само
    if cancel and cancel.is_set():
        return 0
    im = wire.imap_connect(acc, wire.get_password(db, acc.id, account=acc))
    total = 0
    errors = []
    archived_spam = set()
    try:
        progress(tr("{0}: получение списка папок…", acc.name))
        folders = discover_folders(im)
        junk_destination = next((folder.name for folder in folders if folder.view == "spam"), None)
        present = {folder.name for folder in folders}
        skipped = [folder for folder in folders if folder.view == "skip"]
        folders = [folder for folder in folders if folder.view != "skip"]
        folders.sort(key=lambda folder: folder.view != "inbox")
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
                                                           lock.yield_point, flags_done, history_cancel, condstore, archived_spam,
                                                           headers_only=True)
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
        # All lists are visible before ordinary message bodies/attachments begin.
        body_errors, fresh_count = _download_pending_bodies(db, acc, im, folders, progress, cancel, lock,
                                                           archived_spam, full_history, history_cancel, condstore)
        errors.extend(body_errors)
        total += fresh_count
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
        if not (cancel and cancel.is_set()):
            from .mailops import purge_old_trash
            try:
                purge_old_trash(db, acc, im, cancel=cancel)
            except imaplib.IMAP4.abort:
                raise
            except Exception as error:
                raise MailOperationError(tr("Не удалось очистить корзину: {0}", explain(error).summary)) from error
        unfinished = db.conn().execute("SELECT 1 FROM messages WHERE account_id=? AND body_downloaded=0 "
                                       "AND server_folder IS NOT NULL LIMIT 1", (acc.id,)).fetchone()
        if full_history and not unfinished and not (cancel and cancel.is_set()) and not (history_cancel and history_cancel.is_set()) \
                and db.get_setting(f"history:{acc.id}") != "declined":  # отметку сняли в настройках ящика
            db.set_setting(f"history:{acc.id}", "complete")
        return total
    finally:
        try:
            im.logout()
        except Exception:
            pass
