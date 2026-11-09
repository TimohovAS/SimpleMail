"""Хранение паролей.

По умолчанию — системное хранилище (Windows Credential Manager, Keychain,
Secret Service) через библиотеку keyring. Если оно недоступно, пароль
сохраняется в базе (открытым текстом — об этом предупреждаем в README).
"""
from __future__ import annotations

import threading

from .db import Database

SERVICE = "SimpleMail"
_keyring_lock = threading.RLock()

try:
    import keyring
    from keyring.errors import KeyringError
except Exception:  # keyring не установлен
    keyring = None
    KeyringError = Exception


def _key(account_id: int) -> str:
    return f"account:{account_id}"


def initialize_keyring(background: bool = False) -> None:
    """Load the native backend before IMAP workers can initialize it concurrently.

    background — в отдельном потоке (при запуске это ~0,3 с). Все обращения к keyring
    идут под _keyring_lock, поэтому работа с паролями просто дождётся загрузки."""
    if background:
        threading.Thread(target=initialize_keyring, name="keyring-init", daemon=True).start()
        return
    if keyring is not None:
        try:
            with _keyring_lock:
                keyring.get_keyring()
        except Exception:
            pass  # Password operations retain their existing database fallback.


def set_password(db: Database, account_id: int, password: str) -> bool:
    """Сохранить пароль. True — в защищённом хранилище системы, False — открытым текстом в базе."""
    if keyring is not None:
        try:
            with _keyring_lock:
                keyring.set_password(SERVICE, _key(account_id), password)
            _store_password(db, account_id, "", "keyring")
            return True
        except Exception:
            pass
    _store_password(db, account_id, password, "database")
    return False


def _store_password(db, account_id, password, source):
    with db.conn() as c:
        c.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                      [(f"pw:{account_id}", password), (f"pw_source:{account_id}", source)])


def get_password(db: Database, account_id: int, *, account=None) -> str:
    current = db.account(account_id)
    if account is not None:
        fields = ("auth_method", "email", "username", "imap_host", "imap_port", "imap_security",
                  "smtp_host", "smtp_port", "smtp_security", "oauth_client_id", "oauth_tenant",
                  "oauth_client_secret")
        if current is None or any(getattr(account, field) != getattr(current, field) for field in fields):
            from .i18n import tr
            raise ValueError(tr("Настройки ящика изменились. Повторите операцию."))
    if current and current.auth_method in ("microsoft", "google"):
        from .oauth import access_token
        return access_token(db, current)
    fallback = db.get_setting(f"pw:{account_id}", "") or ""
    source = db.get_setting(f"pw_source:{account_id}")
    # Непустая старая запись тоже означает, что сохранение в keyring не удалось.
    if source == "database" or (source is None and fallback):
        return fallback
    if keyring is not None:
        try:
            with _keyring_lock:
                pw = keyring.get_password(SERVICE, _key(account_id))
            if pw:
                return pw
        except Exception:
            pass
    return fallback


def delete_password(db: Database, account_id: int, *, forget=False) -> None:
    if keyring is not None:
        try:
            with _keyring_lock:
                keyring.delete_password(SERVICE, _key(account_id))
        except Exception:
            pass
    if forget:
        with db.conn() as c:
            c.execute("DELETE FROM settings WHERE key IN (?,?)", (f"pw:{account_id}", f"pw_source:{account_id}"))
    else:
        _store_password(db, account_id, "", "database")
