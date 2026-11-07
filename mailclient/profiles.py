"""Remember the user's active mail database independently of the installed build.

The locator lives outside AppData: MSIX hosts can redirect AppData into their
private cache even for an unpackaged child process. It contains only a path;
accounts, preferences and credentials remain in their existing stores.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from .db import default_data_dir
from .i18n import tr

PROFILE_FILE_ENV = "SIMPLEMAIL_PROFILE_FILE"
PROFILE_VERSION = 1


class ProfileError(RuntimeError):
    """An existing profile must not silently turn into an empty new database."""


def profile_file() -> Path:
    override = os.environ.get(PROFILE_FILE_ENV)
    return Path(override).expanduser() if override else Path.home() / ".simplemail" / "profile.json"


def _invalid_locator(path: Path) -> ProfileError:
    return ProfileError(tr(
        "Файл расположения профиля повреждён:\n{0}\n\n"
        "Восстановите этот файл из резервной копии или укажите папку данных через --data.", path))


def _read_locator() -> Path | None:
    path = profile_file()
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if path.is_symlink():
            raise _invalid_locator(path) from None
        return None
    except (OSError, ValueError) as error:
        raise ProfileError(tr("Не удалось прочитать расположение профиля:\n{0}\n\n{1}",
                              path, error)) from error
    try:
        value = json.loads(content)
        if (not isinstance(value, dict) or type(value.get("version")) is not int
                or value["version"] != PROFILE_VERSION
                or not isinstance(value.get("data_dir"), str) or not value["data_dir"]
                or "\0" in value["data_dir"]):
            raise ValueError("Invalid profile locator")
        directory = Path(value["data_dir"])
        if not directory.is_absolute():
            raise ValueError("Profile path must be absolute")
    except (ValueError, TypeError):
        raise _invalid_locator(path) from None
    return directory


def _data_directory(directory, *, existing: bool) -> Path:
    try:
        path = Path(directory).expanduser()
        if existing and not path.is_absolute():
            raise ValueError(tr("Путь к сохранённой папке данных должен быть абсолютным."))
        database = path / "mail.db"
        if existing:
            try:
                found = database.stat()
            except FileNotFoundError:
                found = None
            if found is None or not stat.S_ISREG(found.st_mode):
                raise ProfileError(tr(
                    "Не найдена база писем:\n{0}\n\n"
                    "Подключите диск с почтой или восстановите папку данных из резервной копии.", database))
        elif path.exists() and not path.is_dir():
            raise NotADirectoryError(str(path))
        # Resolve the FILE, not just its parent. An MSIX overlay can redirect
        # mail.db while resolving its parent still returns the physical folder.
        return database.resolve().parent
    except ProfileError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ProfileError(tr("Не удалось открыть папку данных:\n{0}\n\n{1}",
                              directory, error)) from error


def select_data_dir(explicit=None, fallback=None, isolated: bool = False) -> Path:
    """Choose an explicit profile, remembered profile, installer hint or default.

    Only explicit/default choices may create a new database later in startup.
    A missing remembered profile or legacy installer hint remains an error.
    No directory, database or locator is created by this function.
    """
    if isolated and explicit is None:
        raise ProfileError(tr("Для изолированного профиля укажите папку данных через --data."))
    if explicit is not None:
        return _data_directory(explicit, existing=False)
    remembered = _read_locator()
    if remembered is not None:
        return _data_directory(remembered, existing=True)
    if fallback is not None:
        return _data_directory(fallback, existing=True)
    return _data_directory(default_data_dir(), existing=False)


def remember_profile(db) -> bool:
    """Atomically remember an opened profile after it contains an account.

    The caller must skip this for isolated sessions. Database contents and
    credentials are never changed. Empty temporary profiles leave the current
    locator intact; write failures raise ProfileError for a visible warning.
    """
    if db.conn().execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None:
        return False
    target = profile_file()
    temporary = None
    try:
        directory = Path(db.path).resolve(strict=True).parent
        payload = {"version": PROFILE_VERSION, "data_dir": str(directory)}
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".profile-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except (OSError, ValueError) as error:
        raise ProfileError(tr("Не удалось запомнить папку данных:\n{0}\n\n{1}",
                              target, error)) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return True
