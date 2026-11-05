"""Microsoft OAuth2 using MSAL, device sign-in and an encrypted token cache."""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .i18n import tr

SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All", "https://outlook.office.com/SMTP.Send"]
_contexts = {}
_guard = threading.Lock()


class OAuthError(Exception):
    pass


def validate_config(account):
    try:
        uuid.UUID(account.oauth_client_id.strip())
        if account.oauth_tenant not in {"common", "consumers", "organizations"}:
            uuid.UUID(account.oauth_tenant)
    except (ValueError, AttributeError):
        raise OAuthError(tr("Укажите корректный Client ID Microsoft Entra и tenant")) from None
    if account.imap_security == "none" or account.smtp_security == "none":
        raise OAuthError(tr("OAuth2 требует SSL/TLS или STARTTLS"))
    if (account.imap_host.lower().rstrip(".") not in {"outlook.office365.com", "outlook.office.com", "imap-mail.outlook.com"}
            or account.smtp_host.lower().rstrip(".") not in {"smtp.office365.com", "smtp-mail.outlook.com"}):
        raise OAuthError(tr("Для Microsoft OAuth2 используйте серверы outlook.office365.com и smtp.office365.com"))


def _key(db, account):
    identity = (str(Path(db.path).resolve()).casefold(), account.email.casefold(), account.username.casefold(),
                account.oauth_client_id, account.oauth_tenant)
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def cache_root():
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
    return root / "SimpleMail" / "OAuth"


def _cache_location(key):
    return cache_root() / key / "cache.bin"


def _context(db, account):
    import msal
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence
    validate_config(account)
    key = _key(db, account)
    with _guard:
        if key not in _contexts:
            persistent = False
            try:
                path = _cache_location(key)
                path.parent.mkdir(parents=True, exist_ok=True)
                cache = PersistedTokenCache(build_encrypted_persistence(str(path)))
                persistent = True
            except (OSError, RuntimeError, ImportError):
                cache = msal.SerializableTokenCache()
            app = msal.PublicClientApplication(account.oauth_client_id,
                    authority=f"https://login.microsoftonline.com/{account.oauth_tenant}",
                    token_cache=cache, timeout=15)
            _contexts[key] = (app, threading.RLock(), persistent)
        return _contexts[key]


def access_token(db, account):
    app, lock, _ = _context(db, account)
    with lock:
        accounts = app.get_accounts(username=account.username or account.email) or app.get_accounts()
        if len(accounts) != 1:
            raise OAuthError(tr("Войдите в Microsoft в настройках почтового ящика"))
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise OAuthError(tr("Сеанс Microsoft истёк. Повторите вход в настройках ящика."))
        return result["access_token"]


def begin_sign_in(db, account):
    app, lock, _ = _context(db, account)
    with lock:
        flow = app.initiate_device_flow(scopes=SCOPES)
    uri = urlsplit(flow.get("verification_uri", ""))
    if ("user_code" not in flow or uri.scheme != "https" or
            uri.hostname not in {"microsoft.com", "www.microsoft.com", "login.microsoft.com", "login.microsoftonline.com",
                                 "login.live.com"}):
        raise OAuthError(tr("Не удалось начать вход в Microsoft. Проверьте Client ID и разрешения приложения."))
    return flow


def finish_sign_in(db, account, flow, cancel):
    app, _, persistent = _context(db, account)
    # Ожидание кода длится минутами: блокировка не держится, кеш MSAL потокобезопасен.
    if cancel.is_set():
        return None
    result = app.acquire_token_by_device_flow(flow)
    if cancel.is_set():
        return None
    if "access_token" not in result:
        # Не пишем ответы сервера с токенами/кодом устройства в журнал.
        raise OAuthError(tr("Вход в Microsoft не завершён. Повторите вход или проверьте разрешения приложения."))
    return persistent


def logout(db, account):
    # Выход работает и без сети: не создаём MSAL-приложение ради очистки кеша.
    key = _key(db, account)
    with _guard:
        context = _contexts.pop(key, None)
    if context:
        app, lock, _ = context
        with lock:
            try:
                for entry in app.get_accounts():
                    app.remove_account(entry)
            except (OSError, ValueError):
                pass  # повреждённый локальный кеш всё равно очищается ниже
    path = _cache_location(key)
    if path.exists():
        from msal_extensions import build_encrypted_persistence
        build_encrypted_persistence(str(path)).save("{}")
        path.unlink(missing_ok=True)


def sasl_payload(username, token):
    if not username or any(char in username + token for char in "\x01\r\n\0"):
        raise OAuthError(tr("Некорректные данные OAuth2"))
    return f"user={username}\x01auth=Bearer {token}\x01\x01".encode("utf-8")
