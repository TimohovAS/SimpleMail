"""Перечитать уже сохранённые письма из их файлов .eml текущим разборщиком.

Нужен, когда исправлен разбор писем (кодировки и т. п.), а в базе остались темы и тексты,
разобранные старой версией (например «Fiskalni ra�un» вместо «Fiskalni račun»).
Схему базы не меняет. В программу (EXE, установщик) не входит.

Запуск из корня репозитория, при закрытом SimpleMail:

    .venv\\Scripts\\python.exe tools\\reparse_messages.py --dry-run   # показать, что изменится
    .venv\\Scripts\\python.exe tools\\reparse_messages.py             # исправить, с резервной копией

По умолчанию перечитываются только письма с явно испорченным текстом (символ «�»,
управляющие символы, кириллица внутри латинского слова); --all — все письма.
Путь к базе по умолчанию — %APPDATA%\\SimpleMail\\mail.db; другой можно передать аргументом.
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailclient.db import default_data_dir  # noqa: E402
from mailclient.instance import acquire_instance  # noqa: E402
from mailclient.mailparse import parse_message  # noqa: E402
from mailclient.storage import read_eml  # noqa: E402

FIELDS = ("subject", "subject_key", "sender", "recipients", "body_text", "body_html")
_WORD = re.compile(r"[^\W\d_]+")


def looks_broken(text: str | None) -> bool:
    if not text:
        return False
    if "�" in text or any("\x80" <= ch <= "\x9f" for ch in text):
        return True
    return any(any("Ѐ" <= ch <= "ӿ" for ch in w) and any(ch.isascii() for ch in w)
               for w in _WORD.findall(text))


def short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= 70 else text[:67] + "..."


def reparse(c: sqlite3.Connection, everything: bool) -> list[tuple[int, dict]]:
    """Список (id письма, {поле: новое значение}) для писем, у которых разбор изменился."""
    changes = []
    rows = c.execute(f"SELECT id, raw, raw_path, {', '.join(FIELDS)} FROM messages").fetchall()
    for row in rows:
        if not everything and not any(looks_broken(row[f]) for f in ("subject", "sender", "recipients", "body_text")):
            continue
        raw = row["raw"] or read_eml(row["raw_path"])
        if not raw:
            print(f"  письмо {row['id']}: нет файла {row['raw_path']}, пропущено")
            continue
        parsed = parse_message(raw)
        new = {f: getattr(parsed, f) for f in FIELDS if (getattr(parsed, f) or "") != (row[f] or "")}
        if new:
            changes.append((row["id"], new))
            print(f"  письмо {row['id']}: «{short(row['subject'])}»")
            for f, value in new.items():
                if f in ("subject", "sender", "recipients"):
                    print(f"      {f}: «{short(row[f])}» → «{short(value)}»")
            if {"body_text", "body_html"} & new.keys():
                print("      текст письма исправлен")
    return changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("database", nargs="?", type=Path, default=default_data_dir() / "mail.db")
    ap.add_argument("--dry-run", action="store_true", help="только показать изменения, базу не менять")
    ap.add_argument("--all", action="store_true", help="перечитать все письма, а не только испорченные")
    args = ap.parse_args()
    path = args.database.expanduser().resolve()
    if not path.is_file():
        print(f"Нет базы: {path}")
        return 2
    lease = acquire_instance(path.parent)  # держим до конца: SimpleMail не запустится во время правки
    if lease is None:
        print("SimpleMail запущен: закройте его (и в области уведомлений) и запустите скрипт снова.")
        return 3
    c = sqlite3.connect(path, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        changes = reparse(c, args.all)
        print(f"Изменится писем: {len(changes)}.")
        if args.dry_run or not changes:
            return 0
        backup = path.with_name(f"{path.name}.before-reparse-{datetime.now():%Y%m%d-%H%M%S}")
        with sqlite3.connect(backup) as copy:
            c.backup(copy)
        copy.close()
        print(f"Резервная копия: {backup}")
        with c:
            for message_id, new in changes:
                c.execute(f"UPDATE messages SET {', '.join(f'{f}=?' for f in new)} WHERE id=?",
                          (*new.values(), message_id))
        problems = [r[0] for r in c.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            print("ОШИБКА integrity_check:", problems[:10])
            return 5
        print("Готово.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
