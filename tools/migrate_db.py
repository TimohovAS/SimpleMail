"""Обновление существующей базы SimpleMail до схемы текущей версии.

В самой программе миграций нет (до релиза): она открывает только базу своей схемы
(PRAGMA user_version = mailclient.db.SCHEMA_VERSION), а базу другой схемы не трогает.
Этот скрипт в программу (EXE, установщик) не входит. Он содержит только последний
переход схемы; после обновления базы переход отсюда удаляется.

Запуск из корня репозитория, при закрытом SimpleMail:

    .venv\\Scripts\\python.exe tools\\migrate_db.py --dry-run   # на копии базы, оригинал не меняется
    .venv\\Scripts\\python.exe tools\\migrate_db.py             # по-настоящему, с резервной копией

Путь к базе по умолчанию — %APPDATA%\\SimpleMail\\mail.db; другой можно передать аргументом.
"""
import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailclient.db import SCHEMA_VERSION, Database, default_data_dir  # noqa: E402
from mailclient.instance import acquire_instance  # noqa: E402

# ---------- текущий переход: до схемы 16 (значки «ответили»/«переслали») ----------
# Из схемы 0 (0.13.x–0.14.1 со встроенными миграциями) — сначала переписка (схема 14),
# из схемы 14 (база уже обновлена этим скриптом для 0.14.1) — только отметки писем.
FROM_VERSIONS = (0, 14)


def migrate(c: sqlite3.Connection, found: int) -> None:
    if found == 0:
        migrate_threads(c)
    migrate_marks(c)


def migrate_marks(c: sqlite3.Connection) -> None:
    """Схема 16: отметки писем «ответили»/«переслали» (флаги IMAP \\Answered и $Forwarded)."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
    with c:
        for col in ("marks", "pending_marks"):
            if col not in cols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        # Флаги уже скачанных писем: CONDSTORE отдаёт только изменённые после прошлой проверки,
        # поэтому следующая проверка почты один раз перечитает флаги всех писем.
        reset = c.execute("UPDATE folders SET highest_modseq=NULL WHERE highest_modseq IS NOT NULL").rowcount
    print(f"Отметки писем добавлены. Флаги будут перечитаны при следующей проверке почты (папок: {reset}).")


def migrate_threads(c: sqlite3.Connection) -> None:
    """Схема 14: переписка (ключи Message-ID и ссылки на другие письма), поиск без fts_upto."""
    from mailclient.threads import msgid_key, raw_refs, read_header_bytes, subject_key

    cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
    with c:
        for col in ("msgid_key", "subject_key", "refs"):
            if col not in cols:
                c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS message_refs (
                msg INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                ref TEXT NOT NULL,
                PRIMARY KEY(msg, ref)
            );
            CREATE INDEX IF NOT EXISTS ix_message_refs_ref ON message_refs(ref);
            CREATE INDEX IF NOT EXISTS ix_messages_msgid ON messages(msgid_key);
            CREATE INDEX IF NOT EXISTS ix_messages_subject ON messages(subject_key);
        """)

    # Ключи переписки по заголовкам писем (из .eml читаются только заголовки).
    rows = c.execute("SELECT id, message_id, subject, raw_path, substr(raw, 1, 262144) AS head FROM messages "
                     "WHERE refs IS NULL ORDER BY id").fetchall()
    linked = unreadable = 0
    with c:
        for r in rows:
            head = bytes(r["head"]) if r["head"] else read_header_bytes(r["raw_path"])
            unreadable += not head
            try:
                refs = raw_refs(head)
            except Exception:
                refs = []
            linked += bool(refs)
            c.execute("UPDATE messages SET msgid_key=?, subject_key=?, refs=? WHERE id=?",
                      (msgid_key(r["message_id"]), subject_key(r["subject"]), " ".join(refs), r["id"]))
            c.executemany("INSERT OR IGNORE INTO message_refs(msg, ref) VALUES(?, ?)", [(r["id"], k) for k in refs])
    print(f"Писем разобрано: {len(rows)}, со ссылками на другие письма: {linked}, без файла/заголовков: {unreadable}")

    # Поисковый индекс: триггеры без условия fts_upto (фоновой достройки индекса больше нет).
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='messages_fts'").fetchone():
        upto = c.execute("SELECT value FROM settings WHERE key='fts_upto'").fetchone()
        with c:
            for name in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
                c.execute(f"DROP TRIGGER IF EXISTS {name}")
            if upto is None or upto[0] != "9223372036854775807":
                c.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                print("Поисковый индекс был недостроен — перестроен.")
        # Новые триггеры создаст программа из FTS_SCHEMA при проверке ниже.
    contacts = c.execute("SELECT value FROM settings WHERE key='contacts_upto'").fetchone()
    if contacts and contacts[0] != "0":
        print(f"ВНИМАНИЕ: адресная книга не была достроена (contacts_upto={contacts[0]}).")
    with c:
        c.execute("DELETE FROM settings WHERE key IN ('fts_upto', 'contacts_upto')")
    # Остатки ещё более старых версий: программа их больше не дозаполняет — только сообщаем.
    for sql, text in (("SELECT count(*) FROM messages WHERE size IS NULL", "писем без размера"),
                      ("SELECT count(*) FROM accounts WHERE storage_dir=''", "ящиков без папки хранения"),
                      ("SELECT count(*) FROM accounts WHERE signature!=''", "ящиков со старой подписью")):
        if (count := c.execute(sql).fetchone()[0]):
            print(f"ВНИМАНИЕ: {text}: {count}")
# ---------- конец перехода ----------


def schema_of(path: Path) -> int:
    c = sqlite3.connect(path, timeout=5)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def run(path: Path, found: int) -> int:
    c = sqlite3.connect(path, timeout=5)
    c.row_factory = sqlite3.Row
    started = time.monotonic()
    try:
        migrate(c, found)
        problems = [r[0] for r in c.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            print("ОШИБКА integrity_check:", problems[:10])
            return 5
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()
    finally:
        c.close()
    db = Database(path)  # проверка: база открывается текущей версией программы
    try:
        if db.fts:
            db.conn().execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1)")
        last = db.conn().execute("SELECT id FROM messages WHERE folder='inbox' ORDER BY date_ts DESC LIMIT 200").fetchall()
        sizes = [len(db.thread(r["id"])) for r in last]
        print(f"Проверка: база открывается, переписка у последних {len(sizes)} писем — до {max(sizes, default=0)} писем.")
    finally:
        db.close()
    print(f"Готово за {time.monotonic() - started:.1f} с. Схема базы: {SCHEMA_VERSION}.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("database", nargs="?", type=Path, default=default_data_dir() / "mail.db")
    ap.add_argument("--dry-run", action="store_true", help="проверить на копии базы, оригинал не менять")
    args = ap.parse_args()
    path = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"Нет базы: {path}")
        return 2
    lease = acquire_instance(path.parent)  # держим до конца: SimpleMail не запустится во время обновления
    if lease is None:
        print("SimpleMail запущен: закройте его (и в области уведомлений) и запустите скрипт снова.")
        return 3
    found = schema_of(path)
    if found == SCHEMA_VERSION:
        print(f"База уже в схеме {SCHEMA_VERSION}, менять нечего.")
        return 0
    if found not in FROM_VERSIONS:
        print(f"Схема базы {found}, программе нужна {SCHEMA_VERSION}: в этом скрипте такого перехода нет "
              f"(есть: {', '.join(map(str, FROM_VERSIONS)) or 'нет'} → {SCHEMA_VERSION}).")
        return 4
    source = sqlite3.connect(path)
    try:
        if args.dry_run:
            work = Path(tempfile.mkdtemp(prefix="simplemail-migrate-"))
            target = work / path.name
            print(f"Пробный запуск на копии: {target}")
        else:
            target = path.with_name(f"{path.name}.before-schema{SCHEMA_VERSION}-{datetime.now():%Y%m%d-%H%M%S}")
            print(f"Резервная копия: {target}")
        with sqlite3.connect(target) as copy:
            source.backup(copy)
        copy.close()
    finally:
        source.close()
    if args.dry_run:
        try:
            return run(target, found)
        finally:
            shutil.rmtree(target.parent, ignore_errors=True)
    try:
        code = run(path, found)
    except Exception:
        code = 1
        traceback.print_exc()
    if code:
        print(f"База не обновлена. Резервная копия: {target}\n"
              "SimpleMail эту базу не откроет: верните копию на место mail.db "
              "(удалив mail.db-wal и mail.db-shm) или исправьте ошибку и запустите скрипт снова.")
    return code


if __name__ == "__main__":
    sys.exit(main())
