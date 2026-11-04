"""Локальное хранилище: SQLite.

Каждый поток получает своё соединение (sqlite3 не любит общие соединения
между потоками). Схема создаётся автоматически при первом запуске.
"""
from __future__ import annotations

import os
import sqlite3
import threading
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
    storage_dir   TEXT NOT NULL DEFAULT ''      -- папка с файлами писем (.eml) и вложениями
);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,          -- имя папки на сервере
    uidvalidity INTEGER,
    last_uid    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, name)
);

-- folder: 'inbox' | 'sent' | 'trash'. Спам — это флаг is_spam у писем из inbox.
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    folder       TEXT NOT NULL DEFAULT 'inbox',
    server_folder TEXT,
    uid          INTEGER,
    message_id   TEXT,
    subject      TEXT,
    sender       TEXT,
    recipients   TEXT,
    date_ts      INTEGER,               -- unix time
    body_text    TEXT,
    body_html    TEXT,
    raw          BLOB,                  -- старые письма; новые лежат файлами (raw_path)
    raw_path     TEXT,
    seen         INTEGER NOT NULL DEFAULT 0,
    is_spam      INTEGER NOT NULL DEFAULT 0,
    spam_score   REAL,
    spam_reason  TEXT,
    trained_as   INTEGER NOT NULL DEFAULT 0   -- 1 спам, -1 не спам, 0 не обучали
);
CREATE INDEX IF NOT EXISTS ix_messages_list ON messages(account_id, folder, is_spam, date_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_uid ON messages(account_id, server_folder, uid);

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
"""

DEFAULT_SETTINGS = {
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


@dataclass
class Message:
    id: int | None = None
    account_id: int = 0
    folder: str = "inbox"
    server_folder: str | None = None
    uid: int | None = None
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    recipients: str = ""
    date_ts: int = 0
    body_text: str = ""
    body_html: str = ""
    raw: bytes = b""
    raw_path: str | None = None
    seen: bool = False
    is_spam: bool = False
    spam_score: float | None = None
    spam_reason: str = ""
    trained_as: int = 0


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.conn() as c:
            self._migrate(c)
            c.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Обновление базы, созданной старой версией программы."""
        cols = {r[1] for r in c.execute("PRAGMA table_info(accounts)")}
        if cols and "imap_security" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN imap_security TEXT NOT NULL DEFAULT 'ssl'")
            c.execute("UPDATE accounts SET imap_security = CASE WHEN imap_ssl=1 THEN 'ssl' ELSE 'starttls' END")
        if cols and "storage_dir" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN storage_dir TEXT NOT NULL DEFAULT ''")
        mcols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        if mcols and "raw_path" not in mcols:
            c.execute("ALTER TABLE messages ADD COLUMN raw_path TEXT")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
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
        return [self._row_to_account(r) for r in self.conn().execute(sql)]

    def account(self, account_id: int) -> Account | None:
        r = self.conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    @staticmethod
    def _row_to_account(r: sqlite3.Row) -> Account:
        d = dict(r)
        d.pop("imap_ssl", None)
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

    def delete_account(self, account_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

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

    # ---------- messages ----------
    def add_message(self, m: Message) -> int | None:
        """Добавить письмо. Если у ящика задана папка хранения, само письмо пишется туда файлом .eml."""
        d = asdict(m)
        d.pop("id")
        path = None
        row = self.conn().execute("SELECT storage_dir FROM accounts WHERE id=?", (m.account_id,)).fetchone()
        if m.raw and row and row["storage_dir"]:
            path = storage.eml_path(row["storage_dir"], m.folder, m.date_ts, m.uid, m.raw)
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
            m.raw_path = d["raw_path"]
            return cur.lastrowid

    def list_messages(self, account_id: int, view: str) -> list[sqlite3.Row]:
        """view: inbox | spam | sent | trash"""
        base = ("SELECT id, account_id, subject, sender, recipients, date_ts, seen, is_spam, spam_score, spam_reason "
                "FROM messages WHERE account_id=? AND ")
        if view == "inbox":
            where = "folder='inbox' AND is_spam=0"
        elif view == "spam":
            where = "folder='inbox' AND is_spam=1"
        else:
            where = "folder=?"
        params: list = [account_id] + ([view] if view in ("sent", "trash") else [])
        return self.conn().execute(base + where + " ORDER BY date_ts DESC", params).fetchall()

    def unread_count(self, account_id: int, view: str) -> int:
        spam = 1 if view == "spam" else 0
        r = self.conn().execute(
            "SELECT COUNT(*) FROM messages WHERE account_id=? AND folder='inbox' AND is_spam=? AND seen=0",
            (account_id, spam),
        ).fetchone()
        return r[0]

    def message(self, message_id: int) -> Message | None:
        r = self.conn().execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["seen"] = bool(d["seen"])
        d["is_spam"] = bool(d["is_spam"])
        d["raw"] = d["raw"] or storage.read_eml(d["raw_path"])
        return Message(**d)

    def update_message(self, message_id: int, **fields) -> None:
        if not fields:
            return
        with self.conn() as c:
            c.execute(
                f"UPDATE messages SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                list(fields.values()) + [message_id],
            )

    def delete_message(self, message_id: int) -> None:
        r = self.conn().execute("SELECT raw_path FROM messages WHERE id=?", (message_id,)).fetchone()
        with self.conn() as c:
            c.execute("DELETE FROM messages WHERE id=?", (message_id,))
        if r and r["raw_path"]:
            try:
                Path(r["raw_path"]).unlink(missing_ok=True)
            except OSError:
                pass

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
