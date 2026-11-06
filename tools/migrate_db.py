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

# ---------- текущий переход ----------
# База проверки Gmail обновлена до схемы 17 06.11.2026; переход выполнен и удалён.
FROM_VERSIONS: tuple[int, ...] = ()


def migrate(c: sqlite3.Connection, found: int) -> None:
    raise NotImplementedError("переход схемы не описан")
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
