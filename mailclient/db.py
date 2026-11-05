"""Локальное хранилище: SQLite.

Каждый поток получает своё соединение (sqlite3 не любит общие соединения
между потоками). Схема создаётся автоматически при первом запуске.
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
import threading
import time
from email.utils import parseaddr
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    username      TEXT NOT NULL,
    imap_host     TEXT NOT NULL,
    imap_port     INTEGER NOT NULL DEFAULT 993,
    imap_ssl      INTEGER NOT NULL DEFAULT 1,   -- устарело, см. imap_security
    imap_security TEXT NOT NULL DEFAULT 'ssl',  -- ssl | starttls | none
    smtp_host     TEXT NOT NULL,
    smtp_port     INTEGER NOT NULL DEFAULT 465,
    smtp_security TEXT NOT NULL DEFAULT 'ssl',   -- ssl | starttls | none
    enabled       INTEGER NOT NULL DEFAULT 1,
    storage_dir   TEXT NOT NULL DEFAULT '',     -- папка с файлами писем (.eml) и вложениями
    signature     TEXT NOT NULL DEFAULT '',     -- устарело: подписи теперь в таблице signatures
    signature_id  INTEGER,                      -- подпись по умолчанию (signatures.id), NULL — без подписи
    auth_method   TEXT NOT NULL DEFAULT 'password',
    oauth_client_id TEXT NOT NULL DEFAULT '',
    oauth_tenant  TEXT NOT NULL DEFAULT 'common',
    oauth_client_secret TEXT NOT NULL DEFAULT ''  -- Google: секрет настольного приложения (не конфиденциален)
);

CREATE TABLE IF NOT EXISTS signatures (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    html  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,          -- имя папки на сервере
    uidvalidity INTEGER,
    last_uid    INTEGER NOT NULL DEFAULT 0,
    view        TEXT,
    display_name TEXT,
    full_sync   INTEGER NOT NULL DEFAULT 0,
    highest_modseq INTEGER,             -- CONDSTORE: состояние папки на конец прошлой проверки
    exists_count   INTEGER,
    uidnext        INTEGER,
    server_count   INTEGER,             -- писем в папке на сервере при последней проверке
    UNIQUE(account_id, name)
);

-- folder: 'inbox' | 'sent' | 'trash'. Спам — это флаг is_spam у писем из inbox.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder       TEXT NOT NULL DEFAULT 'inbox',
    server_folder TEXT,
    uid          INTEGER,
    uidvalidity  INTEGER,
    trash_origin_folder TEXT,
    trash_origin_server_folder TEXT,
    compose_state TEXT,
    delivery_state TEXT,
    delivery_error TEXT,
    pending_seen INTEGER,
    flagged      INTEGER NOT NULL DEFAULT 0,  -- флажок «Важное» (IMAP \\Flagged)
    pending_flagged INTEGER,                  -- флажок изменён локально, ещё не на сервере
    message_id   TEXT,
    subject      TEXT,
    sender       TEXT,
    recipients   TEXT,
    date_ts      INTEGER,               -- unix time
    body_text    TEXT,
    body_html    TEXT,
    raw          BLOB,                  -- старые письма; новые лежат файлами (raw_path)
    raw_path     TEXT,
    size         INTEGER,               -- размер письма в байтах
    has_att      INTEGER,               -- есть ли вложения (1/0)
    attachments_cached INTEGER NOT NULL DEFAULT 0, -- вложения сохранены в SQLite
    seen         INTEGER NOT NULL DEFAULT 0,
    is_spam      INTEGER NOT NULL DEFAULT 0,
    spam_score   REAL,
    spam_reason  TEXT,
    trained_as   INTEGER NOT NULL DEFAULT 0,  -- 1 спам, -1 не спам, 0 не обучали
    msgid_key    TEXT,                  -- Message-ID без <> в нижнем регистре (для переписки)
    subject_key  TEXT,                  -- тема без Re:/Fwd: (запасной способ собрать переписку)
    refs         TEXT                   -- In-Reply-To/References через пробел; NULL — ещё не разобраны
);
CREATE INDEX IF NOT EXISTS ix_messages_list ON messages(account_id, folder, is_spam, date_ts);
CREATE INDEX IF NOT EXISTS ix_messages_msgid ON messages(msgid_key);
CREATE INDEX IF NOT EXISTS ix_messages_subject ON messages(subject_key);

-- На какие письма ссылается письмо (In-Reply-To, References): поиск ответов на письмо.
CREATE TABLE IF NOT EXISTS message_refs (
    msg INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    PRIMARY KEY(msg, ref)
);
CREATE INDEX IF NOT EXISTS ix_message_refs_ref ON message_refs(ref);
CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_identity ON messages(account_id, server_folder, coalesce(uidvalidity,0), uid);

-- Письма, для которых пользователь нажал «Показать картинки».
-- Ключ — Message-ID и адрес отправителя: разрешение не теряется при перемещении письма
-- между папками, повторной загрузке с сервера и для копий одного письма в разных папках Gmail.
CREATE TABLE IF NOT EXISTS remote_images_allowed (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    msg_key    TEXT NOT NULL,
    PRIMARY KEY(account_id, msg_key)
);

CREATE TABLE IF NOT EXISTS attachments (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    data       BLOB NOT NULL,
    PRIMARY KEY(message_id, position)
);

-- kind: 'white' | 'black'; field: 'from' | 'subject' | 'body'
CREATE TABLE IF NOT EXISTS spam_rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    field   TEXT NOT NULL,
    pattern TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bayes_tokens (
    token TEXT PRIMARY KEY,
    spam  INTEGER NOT NULL DEFAULT 0,
    ham   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Адресная книга: заполняется сама (кому писали, кто писал нам), правится вручную.
CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL,
    email_key      TEXT NOT NULL UNIQUE,         -- адрес в нижнем регистре
    name           TEXT NOT NULL DEFAULT '',
    manual         INTEGER NOT NULL DEFAULT 0,   -- добавлен или исправлен вручную: имя не меняется само
    hidden         INTEGER NOT NULL DEFAULT 0,   -- удалён пользователем: сам больше не появляется
    sent_count     INTEGER NOT NULL DEFAULT 0,   -- писем этому адресату
    received_count INTEGER NOT NULL DEFAULT 0,   -- писем от него
    last_ts        INTEGER NOT NULL DEFAULT 0
);
"""

# Полнотекстовый поиск (FTS5) по теме, адресам и тексту писем. Индекс строится в фоне
# пачками; триггеры обновляют только уже проиндексированные строки (id <= fts_upto),
# остальные дойдут до индекса при фоновой постройке. FTS_DONE — индекс полный.
FTS_DONE = 9223372036854775807
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients, body_text,
    content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages
WHEN new.id <= (SELECT CAST(value AS INTEGER) FROM settings WHERE key='fts_upto') BEGIN
    INSERT INTO messages_fts(rowid, subject, sender, recipients, body_text)
    VALUES (new.id, new.subject, new.sender, new.recipients, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages
WHEN old.id <= (SELECT CAST(value AS INTEGER) FROM settings WHERE key='fts_upto') BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients, body_text)
    VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF subject, sender, recipients, body_text ON messages
WHEN old.id <= (SELECT CAST(value AS INTEGER) FROM settings WHERE key='fts_upto') BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients, body_text)
    VALUES ('delete', old.id, old.subject, old.sender, old.recipients, old.body_text);
    INSERT INTO messages_fts(rowid, subject, sender, recipients, body_text)
    VALUES (new.id, new.subject, new.sender, new.recipients, new.body_text);
END;
"""


def fts_query(text: str) -> str:
    """Запрос FTS5: все слова, каждое как начало слова («иван» найдёт «Иванов»)."""
    words = re.findall(r"\w+", text.casefold())
    return " ".join(f'"{word}"*' for word in words)


DEFAULT_SETTINGS = {
    # The interface language is initialized at startup from the installer.
    "spam_threshold": "0.90",      # выше — спам
    "check_interval_min": "10",    # автопроверка почты, 0 — выключено
    "initial_fetch_limit": "300",  # сколько последних писем качать при первой синхронизации
    "bayes_spam_docs": "0",
    "bayes_ham_docs": "0",
}


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "SimpleMail"


@dataclass
class Account:
    id: int | None = None
    name: str = ""
    email: str = ""
    username: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"   # ssl | starttls | none
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_security: str = "ssl"
    enabled: bool = True
    storage_dir: str = ""
    signature_id: int | None = None   # подпись по умолчанию
    auth_method: str = "password"
    oauth_client_id: str = ""
    oauth_tenant: str = "common"
    oauth_client_secret: str = ""


@dataclass
class Contact:
    id: int | None = None
    email: str = ""
    name: str = ""
    manual: bool = False
    sent_count: int = 0
    received_count: int = 0
    last_ts: int = 0


@dataclass
class Signature:
    id: int | None = None
    name: str = ""
    html: str = ""


@dataclass
class Message:
    id: int | None = None
    account_id: int = 0
    folder: str = "inbox"
    server_folder: str | None = None
    uid: int | None = None
    uidvalidity: int | None = None
    trash_origin_folder: str | None = None
    trash_origin_server_folder: str | None = None
    compose_state: str | None = None
    delivery_state: str | None = None
    delivery_error: str | None = None
    pending_seen: int | None = None
    flagged: bool = False
    pending_flagged: int | None = None
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date_ts: int = 0
    body_text: str = ""
    body_html: str = ""
    raw: bytes = b""
    raw_path: str | None = None
    size: int | None = None
    has_att: int | None = None
    attachments_cached: bool = False
    seen: bool = False
    is_spam: bool = False
    spam_score: float | None = None
    spam_reason: str = ""
    trained_as: int = 0
    msgid_key: str | None = None
    subject_key: str | None = None
    refs: str | None = None


# Папки, письма из которых не показываются в переписке (кроме самого открытого письма).
THREAD_HIDDEN_FOLDERS = ("drafts", "outbox", "trash", "spam")
THREAD_FOLDER_RANK = {"inbox": 0, "sent": 1, "archive": 2}
THREAD_LIMIT = 100


def _has_attachments(raw: bytes) -> bool:
    from .mailparse import attachments
    try:
        return any(True for _ in attachments(raw))
    except Exception:
        return False


class SyncPreempted(Exception):
    """Синхронизация уступает ящик действию пользователя и продолжит с того же места."""


class AccountLock:
    """Блокировка серверного ящика с приоритетом действий пользователя.

    Отправка, удаление, перемещение и пометки берут её как обычную RLock. Синхронизация
    между шагами вызывает yield_point(): если кто-то ждёт ящик, она бросает SyncPreempted,
    отпускает блокировку и продолжает после действия пользователя.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._guard = threading.Condition()
        self._waiting = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._guard:
            self._waiting += 1
        try:
            return self._lock.acquire(blocking, timeout)
        finally:
            with self._guard:
                self._waiting -= 1
                self._guard.notify_all()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def contended(self) -> bool:
        """Кто-то из других потоков ждёт этот ящик."""
        with self._guard:
            return self._waiting > 0

    def yield_point(self) -> None:
        if self.contended():
            raise SyncPreempted()

    def wait_uncontended(self, timeout: float = 30) -> None:
        """Подождать, пока ожидающие возьмут ящик (вызывать без удержания блокировки)."""
        with self._guard:
            self._guard.wait_for(lambda: self._waiting == 0, timeout)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._account_locks: dict[int, AccountLock] = {}
        self._sync_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        with self.conn() as c:
            self._migrate(c)
            new_contacts = not c.execute("SELECT 1 FROM sqlite_master WHERE name='contacts'").fetchone()
            c.executescript(SCHEMA)
            if new_contacts:
                # Письма, скачанные раньше, разберёт build_contacts(); новые записываются сразу.
                upto = c.execute("SELECT coalesce(max(id), 0) FROM messages").fetchone()[0]
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('contacts_upto', ?)", (str(upto),))
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
            self._migrate_signatures(c)
        self.fts = self._create_fts()

    def _create_fts(self) -> bool:
        """False — SQLite без FTS5: поиск работает по-старому, перебором."""
        try:
            with self.conn() as c:
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('fts_upto', '0')")
                c.executescript(FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False

    def search_index_ready(self) -> bool:
        return self.fts and self.get_setting("fts_upto") == str(FTS_DONE)

    def build_search_index(self, batch: int = 500, cancel=None) -> int:
        """Достроить поисковый индекс пачками (каждая — отдельная короткая транзакция,
        чтобы не задерживать запись писем). Возвращает число проиндексированных писем."""
        if not self.fts:
            return 0
        done = 0
        c = self.conn()
        while not (cancel and cancel.is_set()):
            c.execute("BEGIN IMMEDIATE")
            try:
                upto = int(c.execute("SELECT value FROM settings WHERE key='fts_upto'").fetchone()[0])
                if upto == FTS_DONE:
                    c.commit()
                    break
                ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE id>? ORDER BY id LIMIT ?", (upto, batch))]
                if ids:
                    c.execute("INSERT INTO messages_fts(rowid, subject, sender, recipients, body_text) "
                              "SELECT id, subject, sender, recipients, body_text FROM messages WHERE id>? AND id<=?",
                              (upto, ids[-1]))
                c.execute("UPDATE settings SET value=? WHERE key='fts_upto'", (str(ids[-1] if ids else FTS_DONE),))
                c.commit()
            except BaseException:
                c.rollback()
                raise
            done += len(ids)
            if not ids:
                break
        return done

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Обновление базы, созданной старой версией программы."""
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        if cols and "imap_security" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN imap_security TEXT NOT NULL DEFAULT 'ssl'")
            c.execute("UPDATE accounts SET imap_security = CASE WHEN imap_ssl=1 THEN 'ssl' ELSE 'starttls' END")
        if cols and "storage_dir" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN storage_dir TEXT NOT NULL DEFAULT ''")
        if cols and "signature" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
        if cols and "signature_id" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN signature_id INTEGER")
        for column, default in (("auth_method", "password"), ("oauth_client_id", ""), ("oauth_tenant", "common"),
                                ("oauth_client_secret", "")):
            if cols and column not in cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        mcols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        if mcols and "raw_path" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN raw_path TEXT")
        for col in ("size", "has_att"):
            if mcols and col not in mcols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER")
        if mcols and "attachments_cached" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN attachments_cached INTEGER NOT NULL DEFAULT 0")
        for col, sql_type in (("uidvalidity", "INTEGER"), ("trash_origin_folder", "TEXT"),
                              ("trash_origin_server_folder", "TEXT"), ("compose_state", "TEXT"),
                              ("delivery_state", "TEXT"), ("delivery_error", "TEXT"), ("pending_seen", "INTEGER"),
                              ("flagged", "INTEGER NOT NULL DEFAULT 0"), ("pending_flagged", "INTEGER"),
                              ("msgid_key", "TEXT"), ("subject_key", "TEXT"), ("refs", "TEXT")):
            if mcols and col not in mcols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} {sql_type}")
        if mcols:
            c.execute("DROP INDEX IF EXISTS ix_messages_uid")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_identity ON messages(account_id, server_folder, coalesce(uidvalidity,0), uid)")
        fcols = {r[1] for r in c.execute("PRAGMA table_info(folders)")}
        for col, kind in (("view", "TEXT"), ("display_name", "TEXT"), ("full_sync", "INTEGER NOT NULL DEFAULT 0"),
                          ("highest_modseq", "INTEGER"), ("exists_count", "INTEGER"), ("uidnext", "INTEGER"), ("server_count", "INTEGER")):
            if fcols and col not in fcols:
                c.execute(f"ALTER TABLE folders ADD COLUMN {col} {kind}")

    @staticmethod
    def _migrate_signatures(c: sqlite3.Connection) -> None:
        """Подписи из настроек ящиков (версия 0.4) переносим в общий список подписей."""
        rows = c.execute("SELECT id, name, email, signature FROM accounts WHERE signature != ''").fetchall()
        for r in rows:
            same = c.execute("SELECT id FROM signatures WHERE html=?", (r["signature"],)).fetchone()
            if same:
                sid = same["id"]
            else:
                title = r["name"] if r["name"] and r["name"] != r["email"] else r["email"]
                sid = c.execute("INSERT INTO signatures(name, html) VALUES(?, ?)",
                                (f"Подпись {title}".strip(), r["signature"])).lastrowid
            c.execute("UPDATE accounts SET signature_id=?, signature='' WHERE id=?", (sid, r["id"]))

    def account_lock(self, account_id: int) -> AccountLock:
        """Сериализовать изменения одного серверного ящика (синхронизация уступает пользователю)."""
        with self._locks_guard:
            return self._account_locks.setdefault(account_id, AccountLock())

    def sync_lock(self, account_id: int) -> threading.Lock:
        """Не более одной синхронизации ящика одновременно."""
        with self._locks_guard:
            return self._sync_locks.setdefault(account_id, threading.Lock())

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ---------- settings ----------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, str(value)))

    # ---------- accounts ----------
    def accounts(self, only_enabled: bool = False) -> list[Account]:
        sql = "SELECT * FROM accounts" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        accounts = [self._row_to_account(r) for r in self.conn().execute(sql)]
        try:
            order = json.loads(self.get_setting("account_order", "[]"))
        except (ValueError, TypeError):
            return accounts
        if not isinstance(order, list) or any(type(aid) is not int for aid in order):
            return accounts
        positions = {aid: i for i, aid in enumerate(order)}
        # Новые ящики добавляются после сохранённого списка; удалённые пропускаются.
        return sorted(accounts, key=lambda a: (positions.get(a.id, len(order)), a.id))

    def set_account_order(self, account_ids: list[int]) -> None:
        """Сохраняет порядок ящиков, не меняя их ID и связанных данных."""
        account_ids = list(account_ids)
        with self.conn() as c:
            existing = {r[0] for r in c.execute("SELECT id FROM accounts")}
            if len(account_ids) != len(existing) or set(account_ids) != existing:
                raise ValueError("Account order must contain every account ID exactly once")
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('account_order', ?)",
                      (json.dumps(account_ids),))

    def account(self, account_id: int) -> Account | None:
        r = self.conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    @staticmethod
    def _row_to_account(r: sqlite3.Row) -> Account:
        d = dict(r)
        d.pop("imap_ssl", None)
        d.pop("signature", None)
        d["enabled"] = bool(d["enabled"])
        return Account(**d)

    def save_account(self, a: Account) -> int:
        d = asdict(a)
        d.pop("id")
        d["imap_ssl"] = int(a.imap_security == "ssl")
        cols = list(d)
        with self.conn() as c:
            if a.id is None:
                cur = c.execute(
                    f"INSERT INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                    [d[k] for k in cols],
                )
                a.id = cur.lastrowid
            else:
                c.execute(
                    f"UPDATE accounts SET {','.join(k + '=?' for k in cols)} WHERE id=?",
                    [d[k] for k in cols] + [a.id],
                )
        return a.id

    def delete_account(self, account_id: int, *, delete_files=False) -> list[str]:
        """Удалить данные ящика; по явному выбору — только его отслеживаемые .eml."""
        failed = []
        with self.account_lock(account_id):
            account = self.account(account_id)
            paths = [Path(row[0]) for row in self.conn().execute(
                "SELECT DISTINCT raw_path FROM messages WHERE account_id=? AND raw_path IS NOT NULL", (account_id,))]
            with self.conn() as c:
                c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                c.execute("DELETE FROM settings WHERE key IN (?,?,?,?,?)",
                          (f"pw:{account_id}", f"pw_source:{account_id}", f"history:{account_id}",
                           f"trash_days:{account_id}", f"trash_since:{account_id}"))
                c.execute("DELETE FROM settings WHERE key LIKE ?", (f"history_skipped:{account_id}:%",))
            if delete_files and account and account.storage_dir:
                root = Path(account.storage_dir).resolve()
                shared = {Path(row[0]).resolve() for row in self.conn().execute(
                    "SELECT DISTINCT raw_path FROM messages WHERE raw_path IS NOT NULL")}
                for path in paths:
                    resolved = path.resolve()
                    if path.suffix.lower() != ".eml" or not resolved.is_relative_to(root) or resolved in shared:
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        failed.append(str(path))
        return failed

    # ---------- подписи ----------
    def signatures(self) -> list[Signature]:
        return [Signature(**dict(r)) for r in
                self.conn().execute("SELECT id, name, html FROM signatures ORDER BY name COLLATE NOCASE, id")]

    def signature(self, signature_id: int | None) -> Signature | None:
        if not signature_id:
            return None
        r = self.conn().execute("SELECT id, name, html FROM signatures WHERE id=?", (signature_id,)).fetchone()
        return Signature(**dict(r)) if r else None

    def save_signature(self, s: Signature) -> int:
        with self.conn() as c:
            if s.id is None:
                s.id = c.execute("INSERT INTO signatures(name, html) VALUES(?, ?)", (s.name, s.html)).lastrowid
            else:
                c.execute("UPDATE signatures SET name=?, html=? WHERE id=?", (s.name, s.html, s.id))
        return s.id

    def delete_signature(self, signature_id: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE accounts SET signature_id=NULL WHERE signature_id=?", (signature_id,))
            c.execute("DELETE FROM signatures WHERE id=?", (signature_id,))

    def account_signature_html(self, account_id: int | None) -> str:
        """HTML подписи по умолчанию для ящика ('' — без подписи)."""
        acc = self.account(account_id) if account_id else None
        sig = self.signature(acc.signature_id) if acc else None
        return sig.html if sig else ""

    # ---------- folders (состояние синхронизации) ----------
    def folder_state(self, account_id: int, name: str) -> tuple[int | None, int]:
        r = self.conn().execute(
            "SELECT uidvalidity, last_uid FROM folders WHERE account_id=? AND name=?", (account_id, name)
        ).fetchone()
        return (r["uidvalidity"], r["last_uid"]) if r else (None, 0)

    def set_folder_state(self, account_id: int, name: str, uidvalidity: int, last_uid: int) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO folders(account_id, name, uidvalidity, last_uid) VALUES(?,?,?,?) "
                "ON CONFLICT(account_id, name) DO UPDATE SET uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid",
                (account_id, name, uidvalidity, last_uid),
            )

    def folder_marks(self, account_id: int, name: str) -> tuple:
        """(uidvalidity, highest_modseq, exists_count, uidnext, full_sync) прошлой полной проверки папки."""
        r = self.conn().execute("SELECT uidvalidity, highest_modseq, exists_count, uidnext, full_sync FROM folders "
                                "WHERE account_id=? AND name=?", (account_id, name)).fetchone()
        return tuple(r) if r else (None, None, None, None, 0)

    def set_folder_marks(self, account_id: int, name: str, modseq, exists, uidnext) -> None:
        with self.conn() as c:
            c.execute("UPDATE folders SET highest_modseq=?, exists_count=?, uidnext=? WHERE account_id=? AND name=?",
                      (modseq, exists, uidnext, account_id, name))

    def set_server_count(self, account_id: int, name: str, count: int) -> None:
        with self.conn() as c:
            c.execute("UPDATE folders SET server_count=? WHERE account_id=? AND name=?", (count, account_id, name))

    def history_progress(self, account_id: int) -> tuple[int, int]:
        """(скачано, всего на сервере) по папкам, которые синхронизируются."""
        row = self.conn().execute(
            "SELECT coalesce(sum(f.server_count), 0), coalesce(sum((SELECT count(*) FROM messages m "
            " WHERE m.account_id=f.account_id AND m.server_folder=f.name AND m.uid IS NOT NULL "
            " AND (m.uidvalidity IS NULL OR f.uidvalidity IS NULL OR m.uidvalidity=f.uidvalidity))), 0) "
            "FROM folders f WHERE f.account_id=? AND f.server_count IS NOT NULL", (account_id,)).fetchone()
        total, downloaded = int(row[0]), int(row[1])
        return min(downloaded, total), total

    # ---------- messages ----------
    def add_message(self, m: Message) -> int | None:
        """Сохранить письмо, вложения в SQLite и копию .eml в папке ящика."""
        from .mailparse import attachments

        self._fill_thread_keys(m)
        d = asdict(m)
        d.pop("id")
        files = attachments(m.raw) if m.raw else []
        d["attachments_cached"] = int(bool(m.raw))
        if m.raw:
            d["size"], d["has_att"] = len(m.raw), int(bool(files))
        path = None
        row = self.conn().execute("SELECT storage_dir FROM accounts WHERE id=?", (m.account_id,)).fetchone()
        if m.raw and row and row["storage_dir"]:
            path = storage.eml_path(row["storage_dir"], m.folder, m.date_ts, m.uid, m.raw,
                                    f"{m.server_folder}:{m.uidvalidity}" if m.server_folder else "")
            d["raw"], d["raw_path"] = None, str(path)
        cols = list(d)
        with self.conn() as c:
            cur = c.execute(
                f"INSERT OR IGNORE INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [d[k] for k in cols],
            )
            if not cur.rowcount:
                return None
            if path:
                storage.write_eml(path, m.raw)  # при ошибке записи транзакция откатится
            self._insert_attachments(c, cur.lastrowid, files)
            self._index_refs(c, cur.lastrowid, m.refs)
            self._note_message_contacts(c, m.folder, m.is_spam, m.sender, m.recipients, m.date_ts, m.raw)
            m.raw_path = d["raw_path"]
            return cur.lastrowid

    @staticmethod
    def _insert_attachments(c: sqlite3.Connection, message_id: int, files: list[tuple[str, bytes]]) -> None:
        """В базе — только имена вложений (для шапки письма и поиска); содержимое берётся
        из самого письма (.eml), чтобы не хранить каждое вложение дважды."""
        c.executemany(
            "INSERT OR IGNORE INTO attachments(message_id, position, name, data) VALUES(?,?,?,?)",
            [(message_id, position, name, b"") for position, (name, _) in enumerate(files)],
        )

    def attachment_names(self, message_id: int, raw: bytes | None = None) -> list[str]:
        """Имена вложений без чтения их содержимого (для шапки письма)."""
        row = self.conn().execute("SELECT attachments_cached FROM messages WHERE id=?", (message_id,)).fetchone()
        if row and row["attachments_cached"]:
            return [r["name"] for r in self.conn().execute(
                "SELECT name FROM attachments WHERE message_id=? ORDER BY position", (message_id,))]
        from .mailparse import attachments
        return [name for name, _ in attachments(raw)] if raw else []

    def message_attachments(self, message_id: int) -> list[tuple[str, bytes]]:
        """Вложения письма из его .eml (или из письма в базе). Если файла нет — копия
        в базе, сохранённая версией 0.8–0.9 (если она ещё есть)."""
        from .mailparse import attachments
        m = self.message(message_id)
        if not m:
            return []
        if m.raw:
            return attachments(m.raw)
        stored = [(r["name"], r["data"]) for r in self.conn().execute(
            "SELECT name, data FROM attachments WHERE message_id=? ORDER BY position", (message_id,))]
        return stored if any(data for _, data in stored) else []

    def compact(self) -> None:
        """Сжать файл базы после очистки (VACUUM)."""
        self.conn().execute("VACUUM")

    def list_messages(self, account_id: int, view: str) -> list[sqlite3.Row]:
        """view: inbox | spam | sent | trash"""
        base = ("SELECT id, account_id, subject, sender, recipients, date_ts, seen, is_spam, spam_score, spam_reason, "
                "size, has_att "
                "FROM messages WHERE account_id=? AND ")
        if view == "inbox":
            where = "folder='inbox' AND is_spam=0"
        elif view == "spam":
            where = "(folder='inbox' AND is_spam=1 OR folder='spam')"
        else:
            where = "folder=?"
        params: list = [account_id] + ([view] if view not in ("inbox", "spam") else [])
        return self.conn().execute(base + where + " ORDER BY date_ts DESC", params).fetchall()

    def unread_count(self, account_id: int, view: str) -> int:
        where, params = self._view_clause(account_id, view)
        r = self.conn().execute("SELECT COUNT(*) FROM messages m WHERE " + where + " AND seen=0", params).fetchone()
        return r[0]

    @staticmethod
    def _view_clause(account_id=None, view=None):
        clauses, params = ["1=1"], []
        if account_id is not None:
            clauses.append("m.account_id=?"); params.append(account_id)
        if view == "inbox":
            clauses.append("m.folder='inbox' AND m.is_spam=0")
        elif view == "spam":
            clauses.append("(m.folder='spam' OR m.folder='inbox' AND m.is_spam=1)")
        elif view:
            clauses.append("m.folder=?"); params.append(view)
        return " AND ".join(clauses), params

    def search_messages(self, query="", account_id=None, view=None, since=None, until=None, unread=False, limit=None, cancel=None,
                        flagged=False):
        where, params = self._view_clause(account_id, view)
        if unread:
            where += " AND m.seen=0"
        if flagged:
            where += " AND m.flagged=1"
        if since is not None:
            where += " AND m.date_ts>=?"; params.append(since)
        if until is not None:
            where += " AND m.date_ts<?"; params.append(until)
        if query.strip():
            needle = query.strip().casefold()
            match = fts_query(query) if self.search_index_ready() else ""
            if match:
                # Текст письма — через индекс (по началу слов), адреса, тема и имена вложений —
                # по-прежнему по любой части строки: они короткие, перебор по ним быстрый.
                where += (" AND (m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?) OR "
                          "instr(casefold(coalesce(m.sender,'')||' '||coalesce(m.recipients,'')||' '||"
                          "coalesce(m.subject,'')),?)>0 OR EXISTS "
                          "(SELECT 1 FROM attachments x WHERE x.message_id=m.id AND instr(casefold(x.name),?)>0))")
                params += [match, needle, needle]
            else:
                where += (" AND (instr(casefold(coalesce(m.sender,'')||' '||coalesce(m.recipients,'')||' '||"
                          "coalesce(m.subject,'')||' '||coalesce(m.body_text,'')),?)>0 OR EXISTS "
                          "(SELECT 1 FROM attachments x WHERE x.message_id=m.id AND instr(casefold(x.name),?)>0))")
                params += [needle] * 2
        tail = " ORDER BY m.date_ts DESC,m.id DESC"
        if limit is not None:
            tail += " LIMIT ?"; params.append(max(1, int(limit)))
        connection = self.conn()
        if cancel is not None:
            connection.set_progress_handler(lambda: int(cancel.is_set()), 1000)
        try:
            return connection.execute("SELECT m.id,m.account_id,m.folder,m.subject,m.sender,m.recipients,m.date_ts,"
                                      "m.seen,m.flagged,m.is_spam,m.spam_reason,m.size,m.has_att,m.delivery_state,m.delivery_error,a.email AS account_name "
                                      "FROM messages m JOIN accounts a ON a.id=m.account_id WHERE " + where + tail, params).fetchall()
        finally:
            if cancel is not None:
                connection.set_progress_handler(None, 0)

    def server_folders(self, account_id):
        return self.conn().execute("SELECT * FROM folders WHERE account_id=? AND view IS NOT NULL ORDER BY name", (account_id,)).fetchall()

    def save_composition(self, account_id, raw, state, message_id=None, queued=False):
        from .mailparse import parse_message, attachments
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        m = parse_message(raw)
        m.account_id, m.folder, m.seen = account_id, "outbox" if queued else "drafts", True
        m.raw, m.raw_path, m.date_ts = raw, None, int(time.time())
        m.compose_state = json.dumps(state, ensure_ascii=False)
        m.delivery_state = "pending" if queued else None
        if message_id is None:
            # Черновики хранятся в базе целиком, независимо от внешних папок.
            values = asdict(m); values.pop("id")
            values.update(size=len(raw), has_att=int(bool(attachments(raw))), attachments_cached=1)
            with self.conn() as c:
                keys = list(values)
                message_id = c.execute(f"INSERT INTO messages({','.join(keys)}) VALUES({','.join('?'*len(keys))})", list(values.values())).lastrowid
                self._insert_attachments(c, message_id, attachments(raw))
                self._index_refs(c, message_id, m.refs)
        else:
            old = self.message(message_id)
            if not old or old.folder not in ("drafts", "outbox") or old.delivery_state in ("sending", "smtp_sent", "append_pending"):
                raise ValueError("Composition cannot be edited in its current state")
            values = asdict(m); values.pop("id")
            values.update(size=len(raw), has_att=int(bool(attachments(raw))), attachments_cached=1)
            with self.conn() as c:
                c.execute(f"UPDATE messages SET {','.join(k+'=?' for k in values)} WHERE id=?", list(values.values())+[message_id])
                c.execute("DELETE FROM attachments WHERE message_id=?", (message_id,))
                self._insert_attachments(c, message_id, attachments(raw))
                c.execute("DELETE FROM message_refs WHERE msg=?", (message_id,))
                self._index_refs(c, message_id, m.refs)
        return message_id

    def message(self, message_id: int, with_raw: bool = True) -> Message | None:
        """with_raw=False — без чтения файла .eml (для переписки, где нужен только текст)."""
        r = self.conn().execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d.pop("attachments_saved_dir", None)  # совместимость с версией, распаковывавшей файлы
        d["seen"] = bool(d["seen"])
        d["is_spam"] = bool(d["is_spam"])
        d["flagged"] = bool(d.get("flagged"))
        d["raw"] = d["raw"] or (storage.read_eml(d["raw_path"]) if with_raw else b"")
        return Message(**d)

    # ---------- переписка ----------
    @staticmethod
    def _fill_thread_keys(m: Message) -> None:
        from .threads import msgid_key, raw_refs, subject_key
        if m.msgid_key is None:
            m.msgid_key = msgid_key(m.message_id)
        if m.subject_key is None:
            m.subject_key = subject_key(m.subject)
        if m.refs is None and m.raw:
            m.refs = " ".join(raw_refs(m.raw))

    @staticmethod
    def _index_refs(c: sqlite3.Connection, message_id: int, refs: str | None) -> None:
        keys = list(dict.fromkeys((refs or "").split()))
        if keys:
            c.executemany("INSERT OR IGNORE INTO message_refs(msg, ref) VALUES(?, ?)", [(message_id, k) for k in keys])

    def threads_ready(self) -> bool:
        return self.conn().execute("SELECT 1 FROM messages WHERE refs IS NULL LIMIT 1").fetchone() is None

    def build_threads(self, batch: int = 300, cancel=None) -> int:
        """Разобрать In-Reply-To/References у писем, скачанных до версии с перепиской
        (один раз, в фоне, короткими транзакциями). Читаются только заголовки файлов .eml."""
        c = self.conn()
        done, last_id = 0, 0
        while not (cancel and cancel.is_set()):
            rows = c.execute("SELECT id, message_id, subject, raw_path, substr(raw, 1, ?) AS head FROM messages "
                             "WHERE refs IS NULL AND id>? ORDER BY id LIMIT ?",
                             (256 * 1024, last_id, batch)).fetchall()
            if not rows:
                break
            self._parse_thread_rows(c, rows)
            done += len(rows)
            last_id = rows[-1]["id"]
        return done

    def _parse_thread_rows(self, c: sqlite3.Connection, rows) -> None:
        from .threads import msgid_key, raw_refs, read_header_bytes, subject_key
        parsed = []
        for r in rows:
            head = bytes(r["head"]) if r["head"] else read_header_bytes(r["raw_path"])
            try:
                refs = " ".join(raw_refs(head))
            except Exception:
                refs = ""  # испорченные заголовки: письмо просто не связывается по ссылкам
            parsed.append((r["id"], msgid_key(r["message_id"]), subject_key(r["subject"]), refs))
        with c:
            for mid, key, subj, refs in parsed:
                if c.execute("UPDATE messages SET msgid_key=?, subject_key=?, refs=? WHERE id=? AND refs IS NULL",
                             (key, subj, refs, mid)).rowcount:
                    self._index_refs(c, mid, refs)

    def thread(self, message_id: int, limit: int = THREAD_LIMIT) -> list[int]:
        """Письма переписки, к которой относится письмо, по времени (старые сверху).

        Собирает письма из всех папок и ящиков: по Message-ID / In-Reply-To / References
        в обе стороны (на что отвечает письмо и кто ответил на него), а если почтовая
        программа собеседника ссылок не ставит — по теме без «Re:» с общим собеседником.
        Копии одного письма (одинаковый Message-ID в разных папках и ящиках) показываются один раз.
        Черновики, исходящие, корзина и спам не показываются (кроме самого письма).
        """
        from .threads import has_reply_prefix
        c = self.conn()
        # Письмо, ещё не разобранное фоновой задачей (build_threads), разбираем сразу.
        pending = c.execute("SELECT id, message_id, subject, raw_path, substr(raw, 1, ?) AS head FROM messages "
                            "WHERE id=? AND refs IS NULL", (256 * 1024, message_id)).fetchall()
        if pending:
            self._parse_thread_rows(c, pending)
        cols = "id, account_id, folder, msgid_key, subject_key, refs, subject, sender, recipients, date_ts"
        seed = c.execute(f"SELECT {cols} FROM messages WHERE id=?", (message_id,)).fetchone()
        if seed is None:
            return []
        hidden = ",".join("?" * len(THREAD_HIDDEN_FOLDERS))
        found: dict[int, sqlite3.Row] = {seed["id"]: seed}
        keys: set[str] = set()

        def add_keys(row) -> set[str]:
            new = {k for k in [row["msgid_key"] or ""] + (row["refs"] or "").split() if k and k not in keys}
            keys.update(new)
            return new

        def expand(frontier: set[str]) -> None:
            rounds = 0
            while frontier and rounds < 20 and len(found) < limit * 3:
                rounds += 1
                part = list(frontier)[:400]
                frontier = set(list(frontier)[400:])
                marks = ",".join("?" * len(part))
                rows = c.execute(
                    f"SELECT {cols} FROM messages WHERE folder NOT IN ({hidden}) AND "
                    f"(msgid_key IN ({marks}) OR id IN (SELECT msg FROM message_refs WHERE ref IN ({marks})))",
                    list(THREAD_HIDDEN_FOLDERS) + part + part).fetchall()
                for r in rows:
                    if r["id"] not in found:
                        found[r["id"]] = r
                        frontier |= add_keys(r)

        expand(add_keys(seed))
        # Запасной способ — по теме: письма с «Re:»/«Fwd:» хотя бы у одного из писем, общим собеседником
        # (кроме своих ящиков) и без ссылок на известные письма (такие — уже в другой переписке).
        subject = seed["subject_key"] or ""
        if len(subject) >= 3:
            own = self._own_addresses(c)
            members = list(found.values())
            people = set().union(*(self._people(r, own) for r in members))
            reply_like = any(has_reply_prefix(r["subject"]) for r in members)
            dates = [r["date_ts"] for r in members if r["date_ts"]]
            low, high = (min(dates) - 180 * 86400, max(dates) + 180 * 86400) if dates else (0, 2 ** 62)
            extra = set()
            for r in c.execute(f"SELECT {cols} FROM messages WHERE subject_key=? AND folder NOT IN ({hidden}) "
                               f"AND coalesce(date_ts, 0) BETWEEN ? AND ? ORDER BY date_ts DESC LIMIT ?",
                               [subject] + list(THREAD_HIDDEN_FOLDERS) + [low, high, limit * 3]):
                if r["id"] in found:
                    continue
                if (reply_like or has_reply_prefix(r["subject"])) and people & self._people(r, own) \
                        and not self._refs_known(c, r["refs"]):
                    found[r["id"]] = r
                    extra |= add_keys(r)
            expand(extra)
        # Копии одного письма — одна (само открытое письмо, иначе из «Входящих»/«Отправленных»).
        best: dict[str, sqlite3.Row] = {}
        result = []
        for r in found.values():
            key = r["msgid_key"]
            if not key:
                result.append(r)
                continue
            current = best.get(key)
            rank = lambda x: (x["id"] != seed["id"], x["account_id"] != seed["account_id"],
                              THREAD_FOLDER_RANK.get(x["folder"], 3), x["id"])
            if current is None or rank(r) < rank(current):
                best[key] = r
        result += best.values()
        result.sort(key=lambda r: (r["date_ts"] or 0, r["id"]))
        if len(result) > limit:  # очень длинная переписка: последние письма и само открытое
            tail = result[-limit:]
            if not any(r["id"] == seed["id"] for r in tail):
                tail = [seed] + tail[1:]
            result = sorted(tail, key=lambda r: (r["date_ts"] or 0, r["id"]))
        return [r["id"] for r in result]

    @staticmethod
    def _refs_known(c: sqlite3.Connection, refs: str | None) -> bool:
        keys = (refs or "").split()[:200]
        return bool(keys) and c.execute(
            f"SELECT 1 FROM messages WHERE msgid_key IN ({','.join('?' * len(keys))}) LIMIT 1", keys).fetchone() is not None

    @staticmethod
    def _people(row, own) -> set[str]:
        from email.utils import getaddresses
        return {a.strip().casefold() for _, a in getaddresses([row["sender"] or "", row["recipients"] or ""])
                if "@" in a and a.strip().casefold() not in own}

    def update_message(self, message_id: int, **fields) -> None:
        if not fields:
            return
        with self.conn() as c:
            c.execute(
                f"UPDATE messages SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                list(fields.values()) + [message_id],
            )

    # ---------- «Показать картинки» для отдельного письма ----------
    @staticmethod
    def _remote_images_key(m: Message) -> str:
        msgid = (m.message_id or "").strip().lower()
        if not msgid:
            return f"#{m.id}"  # без Message-ID запоминаем по самой записи
        return msgid + "\n" + parseaddr(m.sender or "")[1].lower()

    def remote_images_allowed(self, m: Message) -> bool:
        return self.conn().execute(
            "SELECT 1 FROM remote_images_allowed WHERE account_id=? AND msg_key=?",
            (m.account_id, self._remote_images_key(m))).fetchone() is not None

    def allow_remote_images(self, m: Message) -> None:
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO remote_images_allowed(account_id, msg_key) VALUES(?, ?)",
                      (m.account_id, self._remote_images_key(m)))

    def delete_message(self, message_id: int) -> None:
        r = self.conn().execute("SELECT raw_path FROM messages WHERE id=?", (message_id,)).fetchone()
        with self.conn() as c:
            c.execute("DELETE FROM messages WHERE id=?", (message_id,))
        if r and r["raw_path"]:
            if self.conn().execute("SELECT 1 FROM messages WHERE raw_path=? LIMIT 1", (r["raw_path"],)).fetchone():
                return
            try:
                Path(r["raw_path"]).unlink(missing_ok=True)
            except OSError:
                pass

    def backfill_meta(self) -> int:
        """Посчитать размер и наличие вложений для писем из старых версий."""
        rows = self.conn().execute("SELECT id FROM messages WHERE size IS NULL").fetchall()
        for r in rows:
            m = self.message(r["id"])
            raw = m.raw if m else b""
            self.update_message(r["id"], size=len(raw), has_att=int(_has_attachments(raw)) if raw else 0)
        return len(rows)

    def move_storage(self, account_id: int, old_dir: str, new_dir: str) -> int:
        """Перенести файлы писем ящика в новую папку. Возвращает число перенесённых писем."""
        rows = self.conn().execute(
            "SELECT id, raw_path FROM messages WHERE account_id=? AND raw_path IS NOT NULL", (account_id,)
        ).fetchall()
        moved = 0
        for r in rows:
            new_path = storage.move_file(r["raw_path"], old_dir, new_dir)
            if new_path != r["raw_path"]:
                self.update_message(r["id"], raw_path=new_path)
                moved += 1
        old_att = Path(old_dir) / storage.ATTACHMENTS_DIR
        if old_att.is_dir():
            for f in old_att.iterdir():
                storage.move_file(str(f), old_dir, new_dir)
        return moved

    # ---------- spam rules ----------
    def spam_rules(self) -> list[sqlite3.Row]:
        return self.conn().execute("SELECT * FROM spam_rules ORDER BY kind, field, pattern").fetchall()

    def add_spam_rule(self, kind: str, field_: str, pattern: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO spam_rules(kind, field, pattern) VALUES(?,?,?)", (kind, field_, pattern))

    def delete_spam_rule(self, rule_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM spam_rules WHERE id=?", (rule_id,))

    # ---------- адресная книга ----------
    @staticmethod
    def _own_addresses(c: sqlite3.Connection) -> set[str]:
        return {r[0].strip().casefold() for r in c.execute("SELECT email FROM accounts")}

    @staticmethod
    def _merge_contact(c, address, name, name_ts, sent=0, received=0, last_ts=0, unhide=False) -> None:
        """Добавить адрес в книгу или дополнить запись (имя меняем только у неисправленных вручную)."""
        key = address.casefold()
        row = c.execute("SELECT id, name, manual, hidden, last_ts FROM contacts WHERE email_key=?", (key,)).fetchone()
        if row is None:
            c.execute("INSERT INTO contacts(email, email_key, name, sent_count, received_count, last_ts) VALUES(?,?,?,?,?,?)",
                      (address, key, name, sent, received, last_ts))
            return
        new_name = row["name"]
        if not row["manual"] and name and (not new_name or name_ts >= row["last_ts"]):
            new_name = name
        c.execute("UPDATE contacts SET name=?, sent_count=sent_count+?, received_count=received_count+?, "
                  "last_ts=max(last_ts, ?), hidden=? WHERE id=?",
                  (new_name, sent, received, last_ts, 0 if unhide else row["hidden"], row["id"]))

    @staticmethod
    def _message_addresses(folder, is_spam, sender, recipients, raw=None, own=frozenset()):
        """('sent' | 'received' | None, [(имя, адрес)]) — какие адреса письма идут в книгу."""
        from .contacts import is_service_address, parse_addresses, raw_addresses
        senders = parse_addresses(sender or "")
        if folder == "sent" or senders and senders[0][1].casefold() in own:
            if folder in ("drafts", "outbox"):
                return None, []
            people = raw_addresses(raw) if raw else parse_addresses(recipients or "")
            return "sent", [p for p in people if p[1].casefold() not in own]
        if folder in ("trash", "spam", "drafts", "outbox") or is_spam:
            return None, []
        return "received", [p for p in senders[:1] if p[1].casefold() not in own and not is_service_address(p[1])]

    def _note_message_contacts(self, c, folder, is_spam, sender, recipients, ts, raw=None, unhide=False) -> None:
        try:
            kind, people = self._message_addresses(folder, is_spam, sender, recipients, raw, self._own_addresses(c))
        except Exception:
            return  # книга — дополнение: ошибка разбора адресов не должна мешать сохранению письма
        for name, address in people:
            self._merge_contact(c, address, name, ts or 0, sent=int(kind == "sent"), received=int(kind == "received"),
                                last_ts=ts or 0, unhide=unhide)

    def note_sent_contacts(self, message_id: int) -> None:
        """Письмо отправлено: адресаты (и скрытые) — в книгу, даже если их раньше удалили из неё."""
        m = self.message(message_id)
        if not m or not m.raw:
            return
        from .contacts import raw_addresses
        with self.conn() as c:
            own = self._own_addresses(c)
            for name, address in raw_addresses(m.raw):
                if address.casefold() not in own:
                    self._merge_contact(c, address, name, m.date_ts or int(time.time()), sent=1,
                                        last_ts=m.date_ts or int(time.time()), unhide=True)

    def contacts_ready(self) -> bool:
        return self.get_setting("contacts_upto", "0") == "0"

    def build_contacts(self, batch: int = 2000, cancel=None) -> int:
        """Заполнить книгу по письмам, скачанным до её появления (один раз, в фоне).
        Письма читаем пачками, записываем одной транзакцией; новые письма пишутся сами."""
        upto = int(self.get_setting("contacts_upto", "0") or 0)
        if upto <= 0:
            return 0
        c = self.conn()
        own = self._own_addresses(c)
        found: dict[str, list] = {}  # ключ → [адрес, имя, время имени, отправлено, получено, последнее]
        last_id = done = 0
        while True:
            if cancel and cancel.is_set():
                return 0
            rows = c.execute("SELECT id, folder, is_spam, sender, recipients, date_ts FROM messages "
                             "WHERE id>? AND id<=? ORDER BY id LIMIT ?", (last_id, upto, batch)).fetchall()
            if not rows:
                break
            for r in rows:
                kind, people = self._message_addresses(r["folder"], r["is_spam"], r["sender"], r["recipients"], own=own)
                ts = r["date_ts"] or 0
                for name, address in people:
                    item = found.setdefault(address.casefold(), [address, "", -1, 0, 0, 0])
                    if name and ts >= item[2]:
                        item[1], item[2] = name, ts
                    item[3 if kind == "sent" else 4] += 1
                    item[5] = max(item[5], ts)
            done += len(rows)
            last_id = rows[-1]["id"]
        with c:
            if self.get_setting("contacts_upto", "0") != str(upto):
                return 0  # книгу уже собрал другой поток
            for address, name, name_ts, sent, received, last_ts in found.values():
                self._merge_contact(c, address, name, max(name_ts, 0), sent, received, last_ts)
            c.execute("UPDATE settings SET value='0' WHERE key='contacts_upto'")
        return done

    @staticmethod
    def _contact(r) -> Contact:
        return Contact(id=r["id"], email=r["email"], name=r["name"], manual=bool(r["manual"]),
                       sent_count=r["sent_count"], received_count=r["received_count"], last_ts=r["last_ts"])

    @staticmethod
    def _like(text: str) -> str:
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def contacts(self, query: str = "") -> list[Contact]:
        """Адресная книга (без удалённых), по имени; query — часть имени или адреса."""
        q = self._like(query.strip().casefold())
        rows = self.conn().execute(
            "SELECT * FROM contacts WHERE hidden=0 AND (email_key LIKE ? ESCAPE '\\' OR casefold(name) LIKE ? ESCAPE '\\') "
            "ORDER BY casefold(CASE WHEN name='' THEN email ELSE name END)", (f"%{q}%", f"%{q}%")).fetchall()
        return [self._contact(r) for r in rows]

    def suggest_contacts(self, text: str, limit: int = 8, exclude=()) -> list[Contact]:
        """Подсказки для поля «Кому»: адрес содержит text или слово имени начинается с text.
        Сначала совпадения с начала, затем те, кому писали сами, затем недавние."""
        q = self._like(text.strip().casefold())
        if not q:
            return []
        skip = {address.casefold() for address in exclude}
        rows = self.conn().execute(
            "SELECT *, (email_key LIKE :start ESCAPE '\\' OR casefold(name) LIKE :start ESCAPE '\\') AS starts "
            "FROM contacts WHERE hidden=0 AND (email_key LIKE :any ESCAPE '\\' OR casefold(name) LIKE :start ESCAPE '\\' "
            "OR casefold(name) LIKE :word ESCAPE '\\') "
            "ORDER BY starts DESC, (sent_count > 0 OR manual = 1) DESC, last_ts DESC, sent_count + received_count DESC LIMIT :limit",
            dict(start=f"{q}%", any=f"%{q}%", word=f"% {q}%", limit=limit + len(skip))).fetchall()
        return [self._contact(r) for r in rows if r["email_key"] not in skip][:limit]

    def contact_by_email(self, address: str) -> Contact | None:
        r = self.conn().execute("SELECT * FROM contacts WHERE email_key=? AND hidden=0", (address.strip().casefold(),)).fetchone()
        return self._contact(r) if r else None

    def save_contact(self, contact: Contact) -> int:
        """Добавить или изменить контакт вручную. ValueError — такой адрес уже есть в книге."""
        address = contact.email.strip()
        key = address.casefold()
        with self.conn() as c:
            other = c.execute("SELECT id, hidden FROM contacts WHERE email_key=?", (key,)).fetchone()
            if other and other["id"] != contact.id:
                if not other["hidden"]:
                    raise ValueError(address)
                if contact.id is None:  # возвращаем удалённый раньше адрес
                    contact.id = other["id"]
                else:
                    c.execute("DELETE FROM contacts WHERE id=?", (other["id"],))
            if contact.id is None:
                contact.id = c.execute("INSERT INTO contacts(email, email_key, name, manual) VALUES(?,?,?,1)",
                                       (address, key, contact.name.strip())).lastrowid
            else:
                c.execute("UPDATE contacts SET email=?, email_key=?, name=?, manual=1, hidden=0 WHERE id=?",
                          (address, key, contact.name.strip(), contact.id))
        return contact.id

    def delete_contacts(self, contact_ids) -> None:
        """Удалённые адреса остаются скрытыми: сами по входящим письмам они не вернутся."""
        with self.conn() as c:
            c.executemany("UPDATE contacts SET hidden=1, manual=0 WHERE id=?", [(i,) for i in contact_ids])

    def forget_spam_sender(self, sender: str) -> None:
        """Письмо признано спамом: его отправителя убираем из книги, если ему ничего не писали."""
        from .contacts import parse_addresses
        people = parse_addresses(sender or "")
        if people:
            with self.conn() as c:
                c.execute("DELETE FROM contacts WHERE email_key=? AND sent_count=0 AND manual=0",
                          (people[0][1].casefold(),))
