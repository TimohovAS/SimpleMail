"""Хранение паролей.

По умолчанию — системное хранилище (Windows Credential Manager, Keychain,
Secret Service) через библиотеку keyring. Если оно недоступно, пароль
сохраняется в базе (открытым текстом — об этом предупреждаем в README).
"""
from __future__ import annotations

from .db import Database

SERVICE = "SimpleMail"

try:
    import keyring
    from keyring.errors import KeyringError
except Exception:  # keyring не установлен
    keyring = None
    KeyringError = Exception


def _key(account_id: int) -> str:
    return f"account:{account_id}"


def set_password(db: Database, account_id: int, password: str) -> None:
    if keyring is not None:
        try:
            keyring.set_password(SERVICE, _key(account_id), password)
            db.set_setting("pw:" + str(account_id), "")
            return
        except Exception:
            pass
    db.set_setting("pw:" + str(account_id), password)


def get_password(db: Database, account_id: int) -> str:
    if keyring is not None:
        try:
            pw = keyring.get_password(SERVICE, _key(account_id))
            if pw:
                return pw
        except Exception:
            pass
    return db.get_setting("pw:" + str(account_id), "") or ""


def delete_password(db: Database, account_id: int) -> None:
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE, _key(account_id))
        except Exception:
            pass
    db.set_setting("pw:" + str(account_id), "")
