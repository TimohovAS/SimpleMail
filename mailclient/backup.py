"""Полные резервные копии SQLite и папок ящиков с проверяемым восстановлением."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path, PurePosixPath

from .i18n import tr


def create_backup(db, destination, progress=lambda text: None):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination == Path(db.path).resolve():
        raise ValueError(tr("Резервная копия не может заменить рабочую базу."))
    accounts = db.accounts()
    with ExitStack() as locks, tempfile.TemporaryDirectory() as temp:
        for account in sorted(accounts, key=lambda item: item.id):
            locks.enter_context(db.account_lock(account.id))
        snapshot = Path(temp) / "mail.db"
        with closing(sqlite3.connect(snapshot)) as target:
            db.conn().backup(target)
            # Пароли, сохранённые без хранилища Windows, в резервную копию не попадают.
            target.execute("PRAGMA secure_delete = ON")  # стереть и сами байты, не только запись
            with target:
                target.execute("DELETE FROM settings WHERE key LIKE 'pw:%'")
        manifest = {"format": 1, "accounts": [account.id for account in accounts], "raw_paths": {}}
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".simplemail-backup-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                archive.write(snapshot, "mail.db")
                stored = {}
                excluded = {destination, temporary.resolve(), Path(db.path).resolve(),
                            Path(db.path + "-wal").resolve(), Path(db.path + "-shm").resolve()}
                for account in accounts:
                    root = Path(account.storage_dir).resolve() if account.storage_dir else None
                    if not root or not root.is_dir():
                        continue
                    progress(tr("Резервное копирование: {0}", account.email))
                    for file in root.rglob("*"):
                        if file.is_file() and file.resolve() not in excluded:
                            name = f"accounts/{account.id}/" + file.relative_to(root).as_posix()
                            archive.write(file, name)
                            stored[file.resolve()] = name
                with closing(sqlite3.connect(snapshot)) as copied:
                    copied.row_factory = sqlite3.Row
                    for row in copied.execute("SELECT id,raw,raw_path FROM messages WHERE raw_path IS NOT NULL"):
                        path = Path(row["raw_path"]).resolve()
                        if not path.is_file():
                            if row["raw"]:
                                continue
                            raise FileNotFoundError(tr("Нет исходного файла письма: {0}", str(path)))
                        name = stored.get(path)
                        if name is None:
                            name = f"messages/{row['id']}.eml"
                            archive.write(path, name)
                            stored[path] = name
                        manifest["raw_paths"][str(row["id"])] = name
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return str(destination)


def _extract_archive(archive_path, stage):
    with zipfile.ZipFile(archive_path) as archive:
        names = set()
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or
                    any(":" in part for part in path.parts) or item.filename in names or
                    stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError(tr("Небезопасный путь в резервной копии: {0}", item.filename))
            names.add(item.filename)
            target = stage.joinpath(*path.parts)
            if not target.resolve().is_relative_to(stage.resolve()):
                raise ValueError(tr("Некорректная резервная копия"))
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if not (stage / "manifest.json").is_file():
        raise ValueError(tr("Некорректная резервная копия"))
    manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not isinstance(manifest.get("raw_paths"), dict):
        raise ValueError(tr("Некорректная резервная копия"))
    return manifest


def prepare_restore(db, archive_path, progress=lambda text: None):
    """Подготовить восстановление, не заменяя открытую базу приложения."""
    data_dir = Path(db.path).resolve().parent
    stage = data_dir / "restored" / uuid.uuid4().hex
    stage.mkdir(parents=True)
    try:
        manifest = _extract_archive(archive_path, stage)
        database = stage / "mail.db"
        if not database.is_file():
            raise ValueError(tr("Некорректная резервная копия"))
        with closing(sqlite3.connect(database)) as restored:
            if restored.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(tr("Резервная база не прошла проверку целостности"))
            accounts = [row[0] for row in restored.execute("SELECT id FROM accounts")]
            if set(accounts) != set(manifest["accounts"]):
                raise ValueError(tr("Некорректная резервная копия"))
            with restored:
                for aid in accounts:
                    root = stage / "accounts" / str(aid)
                    root.mkdir(parents=True, exist_ok=True)
                    restored.execute("UPDATE accounts SET storage_dir=? WHERE id=?", (str(root), aid))
                for mid, name in manifest["raw_paths"].items():
                    target = (stage / name).resolve()
                    if not target.is_relative_to(stage) or not target.is_file():
                        raise ValueError(tr("Некорректная резервная копия"))
                    restored.execute("UPDATE messages SET raw_path=? WHERE id=?", (str(target), int(mid)))
                for mid, raw, raw_path in restored.execute("SELECT id,raw,raw_path FROM messages WHERE raw_path IS NOT NULL").fetchall():
                    if str(mid) not in manifest["raw_paths"]:
                        if not raw:
                            raise ValueError(tr("Некорректная резервная копия"))
                        restored.execute("UPDATE messages SET raw_path=NULL WHERE id=?", (mid,))
                # Пароли не входят в копию: переносим текущие для тех же ящиков.
                for aid in accounts:
                    current = db.get_setting(f"pw:{aid}")
                    if current:
                        restored.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (f"pw:{aid}", current))
                restored.execute("UPDATE messages SET delivery_state='uncertain',delivery_error=? WHERE folder='outbox'",
                                 (tr("Письмо восстановлено из резервной копии. Проверьте доставку перед повторной отправкой."),))
        safety = data_dir / f"before-restore-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.smbak"
        create_backup(db, safety, progress)
        (stage / "ready.json").write_text(json.dumps({"safety_backup": str(safety)}), encoding="utf-8")
        return str(stage)
    except Exception:
        # Удаляется только новая временная папка, созданная этим вызовом.
        if stage.resolve().is_relative_to((data_dir / "restored").resolve()):
            shutil.rmtree(stage)
        raise


def install_restore(stage, data_dir):
    """Установить подготовленную базу после завершения прежнего процесса."""
    stage, data_dir = Path(stage).resolve(), Path(data_dir).resolve()
    if not stage.is_relative_to(data_dir / "restored"):
        raise ValueError(tr("Некорректная папка восстановления"))
    marker = json.loads((stage / "ready.json").read_text(encoding="utf-8"))
    if not Path(marker["safety_backup"]).is_file():
        raise ValueError(tr("Нет страховочной резервной копии"))
    with closing(sqlite3.connect((stage / "mail.db").as_uri() + "?mode=ro", uri=True)) as prepared:
        if prepared.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(tr("Резервная база не прошла проверку целостности"))
    database = data_dir / "mail.db"
    if database.exists():
        with closing(sqlite3.connect(database, timeout=3)) as current:
            busy, _, _ = current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy:
                raise RuntimeError(tr("Рабочая база ещё используется приложением"))
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    os.replace(stage / "mail.db", database)
    (stage / "ready.json").unlink()
