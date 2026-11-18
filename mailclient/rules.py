"""Правила для входящих писем: «если отправитель/тема/получатель содержит X — перенести в папку,
отметить важным, отметить прочитанным».

Правила хранятся в настройке mail_rules (JSON), схема базы не меняется. Проверка почты
применяет их к новым письмам во «Входящих» (не к спаму и не при самой первой загрузке ящика);
кнопка «Применить сейчас» в окне правил — ко всем письмам во «Входящих».
"""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field

from .errors import MailOperationError, explain
from .i18n import tr

log = logging.getLogger(__name__)

SETTING = "mail_rules"
FIELDS = {"from": "Отправитель", "to": "Получатель (кому или копия)", "subject": "Тема"}
MATCH_ALL, MATCH_ANY = "all", "any"


@dataclass
class Rule:
    name: str = ""
    enabled: bool = True
    account_id: int | None = None          # None — все ящики
    match: str = MATCH_ALL                 # all — все условия, any — любое
    conditions: list[list[str]] = field(default_factory=list)  # [[поле, текст], ...]
    move_to: str | None = None             # серверная папка ящика account_id
    mark_flagged: bool = False
    mark_read: bool = False
    stop: bool = False                     # следующие правила к письму не применять

    def has_action(self) -> bool:
        return bool(self.move_to or self.mark_flagged or self.mark_read)

    def applies_to(self, account_id: int) -> bool:
        return self.account_id is None or self.account_id == account_id

    def matches(self, message) -> bool:
        conditions = [(f, t.strip()) for f, t in self.conditions if f in FIELDS and t.strip()]
        if not conditions:
            return False
        values = {"from": message.sender, "to": message.recipients, "subject": message.subject}
        results = (text.casefold() in (values[f] or "").casefold() for f, text in conditions)
        return any(results) if self.match == MATCH_ANY else all(results)


@dataclass
class Actions:
    move_to: str | None = None
    mark_flagged: bool = False
    mark_read: bool = False

    def __bool__(self):
        return bool(self.move_to or self.mark_flagged or self.mark_read)


def _rule_from(data) -> Rule | None:
    if not isinstance(data, dict):
        return None
    rule = Rule()
    rule.name = str(data.get("name") or "")
    rule.enabled = bool(data.get("enabled", True))
    account_id = data.get("account_id")
    rule.account_id = account_id if type(account_id) is int else None
    rule.match = MATCH_ANY if data.get("match") == MATCH_ANY else MATCH_ALL
    rule.conditions = [[str(c[0]), str(c[1])] for c in data.get("conditions") or []
                       if isinstance(c, (list, tuple)) and len(c) == 2 and c[0] in FIELDS]
    move_to = data.get("move_to")
    # Папка есть только у правила одного ящика: в разных ящиках папки разные.
    rule.move_to = str(move_to) if move_to and rule.account_id is not None else None
    rule.mark_flagged = bool(data.get("mark_flagged"))
    rule.mark_read = bool(data.get("mark_read"))
    rule.stop = bool(data.get("stop"))
    return rule


def load_rules(db) -> list[Rule]:
    try:
        data = json.loads(db.get_setting(SETTING, "[]") or "[]")
    except (ValueError, TypeError):
        log.warning("Настройка %s испорчена, правила не применяются", SETTING)
        return []
    if not isinstance(data, list):
        return []
    return [rule for rule in map(_rule_from, data) if rule is not None]


def save_rules(db, rules: list[Rule]) -> None:
    db.set_setting(SETTING, json.dumps([asdict(rule) for rule in rules], ensure_ascii=False))


def active_rules(db, account_id: int) -> list[Rule]:
    return [rule for rule in load_rules(db) if rule.enabled and rule.has_action() and rule.applies_to(account_id)]


def actions_for(rules: list[Rule], message) -> Actions:
    """Что сделать с письмом: действия всех подходящих правил по порядку. Папка — первого
    подошедшего правила с папкой; «Остановить» — следующие правила не проверяются."""
    result = Actions()
    for rule in rules:
        if not rule.matches(message):
            continue
        result.mark_flagged = result.mark_flagged or rule.mark_flagged
        result.mark_read = result.mark_read or rule.mark_read
        if rule.move_to and not result.move_to:
            result.move_to = rule.move_to
        if rule.stop:
            break
    return result


def _destination_view(db, account_id: int, server_folder: str) -> str | None:
    row = db.conn().execute("SELECT view FROM folders WHERE account_id=? AND name=? AND view IS NOT NULL",
                            (account_id, server_folder)).fetchone()
    return row["view"] if row else None


def _store_flag(db, im, m, flag: str, column: str) -> None:
    """Флаг письму: сразу в базе (с pending_*, как у действий пользователя), потом на сервере."""
    from .mailops import _selected_uid, mark_seen
    from .protocols import _clear_copies_pending, is_gmail, store_flag
    if column == "pending_seen":
        mark_seen(db, [m.id], True)  # в Gmail — и копиям в метках
    else:
        db.update_message(m.id, flagged=1, pending_flagged=1)
    if not m.server_folder or m.uid is None:
        return
    uid, _ = _selected_uid(im, db, m)
    store_flag(im, [uid], flag, True)
    with db.conn() as c:
        c.execute(f"UPDATE messages SET {column}=NULL WHERE id=? AND {column}=1", (m.id,))
    if is_gmail(db.account(m.account_id)):
        _clear_copies_pending(db, [m.id], column, True)


def apply_to_message(db, im, message_id: int, rules: list[Rule], download_body=None) -> bool:
    """Применить правила к одному письму во «Входящих». True — письмо подошло под правило.

    Вызывать под блокировкой ящика; im — подключение этого ящика. download_body(message) —
    скачать содержимое письма, у которого пока только заголовок: перенос без содержимого
    небезопасен (сервер может не сообщить новый UID), а заодно письмо проверяется на спам."""
    from .mailops import MessageGone, _complete_for_action, _server_move
    m = db.message(message_id)
    if not m or m.folder != "inbox" or m.is_spam:
        return False
    actions = actions_for(rules, m)
    if not actions:
        return False
    if not m.body_downloaded and m.server_folder:
        if download_body is not None:
            download_body(m)
        else:
            _complete_for_action(db, m)
        m = db.message(message_id)
        if not m or not m.body_downloaded or m.folder != "inbox" or m.is_spam:
            return False  # пока качалось содержимое, письмо оказалось спамом или его убрали
    if actions.mark_read and not m.seen:
        _store_flag(db, im, m, "\\Seen", "pending_seen")
    if actions.mark_flagged and not m.flagged:
        _store_flag(db, im, m, "\\Flagged", "pending_flagged")
    if actions.move_to and actions.move_to != m.server_folder:
        view = _destination_view(db, m.account_id, actions.move_to)
        if view is None or view in ("inbox", "drafts", "outbox", "trash", "spam"):
            raise MailOperationError(tr("Папки «{0}» больше нет в ящике.", actions.move_to))
        m = db.message(message_id)
        if not m.server_folder:
            db.update_message(m.id, folder=view)
            return True
        try:
            fields = _server_move(db, m, actions.move_to, connection=im)
        except MessageGone:
            return True  # письмо уже убрали с сервера другой программой — синхронизация это учтёт
        db.update_message(m.id, folder=view, **fields)
    return True


def apply_rules(db, acc, message_ids, *, connection=None, lock=None, cancel=None, download_body=None) -> tuple[int, list[str]]:
    """Применить правила ящика к письмам. Возвращает (сколько писем подошло, ошибки).
    Ошибка одного письма не мешает остальным. Между письмами ящик уступает пользователю."""
    rules = active_rules(db, acc.id)
    if not rules or not message_ids:
        return 0, []
    from .protocols import account_imap
    lock = lock or db.account_lock(acc.id)
    matched, errors = 0, []
    for message_id in message_ids:
        if cancel and cancel.is_set():
            break
        try:
            with lock:
                with (nullcontext(connection) if connection is not None else account_imap(db, acc)) as im:
                    if apply_to_message(db, im, message_id, rules, download_body):
                        matched += 1
        except Exception as error:
            log.warning("Правило не применилось к письму %s: %s", message_id, error)
            summary = explain(error).summary
            if summary not in errors:
                errors.append(summary)
        if hasattr(lock, "wait_uncontended"):
            lock.wait_uncontended()
    return matched, errors


def apply_to_inbox(db, account_ids=None, cancel=None) -> tuple[int, list[str]]:
    """«Применить сейчас»: правила ко всем письмам во «Входящих» (не спам) выбранных ящиков."""
    matched, errors = 0, []
    for acc in db.accounts(only_enabled=True):
        if account_ids is not None and acc.id not in account_ids:
            continue
        if not active_rules(db, acc.id):
            continue
        ids = [row["id"] for row in db.conn().execute(
            "SELECT id FROM messages WHERE account_id=? AND folder='inbox' AND is_spam=0 ORDER BY date_ts DESC",
            (acc.id,))]
        count, failed = apply_rules(db, acc, ids, cancel=cancel)
        matched += count
        errors.extend(f"{acc.name}: {text}" for text in failed)
    return matched, errors
