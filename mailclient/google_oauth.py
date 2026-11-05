"""Вход через Google (OAuth2 для настольных приложений): браузер, адрес возврата 127.0.0.1, PKCE.

Токен обновления хранится как у Microsoft: в %LOCALAPPDATA%\\SimpleMail\\OAuth под защитой
Windows DPAPI; без защищённого хранилища — только в памяти до перезапуска.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from .i18n import tr

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = "openid email https://mail.google.com/"
IMAP_HOSTS = {"imap.gmail.com", "imap.googlemail.com"}
SMTP_HOSTS = {"smtp.gmail.com", "smtp.googlemail.com"}
SIGN_IN_TIMEOUT = 600
_memory = {}          # ключ -> токены, если DPAPI недоступен
_locks = {}
_guard = threading.Lock()


class GoogleAuthError(Exception):
    pass


def client(account):
    """(Client ID, Client secret): указанные в ящике, иначе встроенные в программу."""
    client_id = account.oauth_client_id.strip()
    if client_id:
        return client_id, account.oauth_client_secret.strip()
    from .oauth import default_client
    builtin = default_client("google")
    return builtin["client_id"], builtin["client_secret"]


def validate_config(account):
    client_id, client_secret = client(account)
    if not client_id and not client_secret:
        raise GoogleAuthError(tr("Вход через Google не настроен в этой копии программы. Выберите способ входа "
                                 "«Пароль» и введите пароль приложения Google или укажите свой Client ID."))
    from .oauth import google_client_valid
    if not google_client_valid(client_id, client_secret):
        raise GoogleAuthError(tr("Укажите Client ID и Client secret приложения Google"))
    if account.imap_security == "none" or account.smtp_security == "none":
        raise GoogleAuthError(tr("OAuth2 требует SSL/TLS или STARTTLS"))
    if (account.imap_host.lower().rstrip(".") not in IMAP_HOSTS
            or account.smtp_host.lower().rstrip(".") not in SMTP_HOSTS):
        raise GoogleAuthError(tr("Для входа через Google используйте серверы imap.gmail.com и smtp.gmail.com"))


def _key(db, account):
    from dataclasses import replace
    from .oauth import _key as account_key
    # Ключ по фактическому Client ID: ящик без своего ID и со встроенным — один и тот же сеанс.
    return "google-" + account_key(db, replace(account, oauth_client_id=client(account)[0]))


def _path(key):
    from .oauth import cache_root
    return cache_root() / key / "token.bin"


def _lock(key):
    with _guard:
        return _locks.setdefault(key, threading.RLock())


def _load(key):
    if key in _memory:
        return dict(_memory[key])
    path = _path(key)
    if not path.exists():
        return None
    try:
        from msal_extensions import build_encrypted_persistence
        return json.loads(build_encrypted_persistence(str(path)).load())
    except Exception:
        return None


def _save(key, tokens):
    """True — сохранено под DPAPI; False — только в памяти."""
    try:
        from msal_extensions import build_encrypted_persistence
        path = _path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        build_encrypted_persistence(str(path)).save(json.dumps(tokens))
        _memory.pop(key, None)
        return True
    except Exception:
        _memory[key] = dict(tokens)
        return False


def _post(url, fields):
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("ascii"),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except ValueError:
            return {"error": f"http {error.code}"}


def _id_email(id_token):
    try:
        payload = id_token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data.get("email", "")).casefold()
    except (IndexError, ValueError, AttributeError):
        return ""


class _Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "code" in query or "error" in query:
            self.server.result = {key: values[0] for key, values in query.items()}
            text = tr("Вход выполнен. Вкладку можно закрыть и вернуться в SimpleMail.") \
                if "code" in query else tr("Вход не выполнен. Вернитесь в SimpleMail.")
            body = f"<!doctype html><meta charset='utf-8'><title>SimpleMail</title><p>{text}</p>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # код авторизации не попадает в журнал


def begin_sign_in(db, account):
    validate_config(account)
    server = HTTPServer(("127.0.0.1", 0), _Redirect)
    server.result = None
    server.timeout = 0.5
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client(account)[0], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent", "login_hint": account.username or account.email})
    return {"provider": "google", "auth_url": url, "verification_uri": url, "server": server,
            "verifier": verifier, "state": state, "redirect": redirect,
            "expires_at": time.time() + SIGN_IN_TIMEOUT}


def finish_sign_in(db, account, flow, cancel):
    server = flow["server"]
    try:
        while server.result is None:
            if cancel.is_set() or time.time() > flow.get("expires_at", 0):
                return None
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if result.get("state") != flow["state"] or "code" not in result:
        raise GoogleAuthError(tr("Вход через Google не завершён. Повторите вход."))
    client_id, client_secret = client(account)
    tokens = _post(TOKEN_URL, {
        "client_id": client_id, "client_secret": client_secret,
        "code": result["code"], "code_verifier": flow["verifier"], "redirect_uri": flow["redirect"],
        "grant_type": "authorization_code"})
    if cancel.is_set():
        return None
    if "refresh_token" not in tokens or "access_token" not in tokens:
        raise GoogleAuthError(tr("Вход через Google не завершён. Проверьте Client ID, Client secret и доступ к Gmail."))
    signed_in = _id_email(tokens.get("id_token", ""))
    expected = {value.casefold() for value in (account.email, account.username) if value}
    if signed_in and signed_in not in expected:
        raise GoogleAuthError(tr("Вы вошли в Google как {0}, а ящик — {1}. Войдите в нужный аккаунт.",
                                 signed_in, account.email))
    key = _key(db, account)
    with _lock(key):
        return _save(key, {"refresh_token": tokens["refresh_token"], "access_token": tokens["access_token"],
                           "expires_at": time.time() + int(tokens.get("expires_in", 3600))})


def access_token(db, account):
    validate_config(account)
    key = _key(db, account)
    with _lock(key):
        tokens = _load(key)
        if not tokens or not tokens.get("refresh_token"):
            raise GoogleAuthError(tr("Войдите в Google в настройках почтового ящика"))
        if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 60:
            return tokens["access_token"]
        client_id, client_secret = client(account)
        fresh = _post(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret,
                                  "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token"})
        if "access_token" not in fresh:
            if fresh.get("error") == "invalid_grant":
                logout(db, account, revoke=False)
                raise GoogleAuthError(tr("Сеанс Google истёк. Повторите вход в настройках ящика."))
            raise GoogleAuthError(tr("Не удалось обновить вход Google. Проверьте подключение к интернету."))
        tokens.update(access_token=fresh["access_token"],
                      expires_at=time.time() + int(fresh.get("expires_in", 3600)))
        if fresh.get("refresh_token"):
            tokens["refresh_token"] = fresh["refresh_token"]
        if key in _memory:
            _memory[key] = tokens
        else:
            _save(key, tokens)
        return tokens["access_token"]


def logout(db, account, revoke=True):
    key = _key(db, account)
    with _lock(key):
        tokens = _load(key)
        _memory.pop(key, None)
        path = _path(key)
        if path.exists():
            try:
                from msal_extensions import build_encrypted_persistence
                build_encrypted_persistence(str(path)).save("{}")
            except Exception:
                pass
            path.unlink(missing_ok=True)
    if revoke and tokens and tokens.get("refresh_token"):
        try:
            _post(REVOKE_URL, {"token": tokens["refresh_token"]})
        except OSError:
            pass  # без сети локальный сеанс всё равно удалён
