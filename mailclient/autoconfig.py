"""Автоопределение серверов, портов и шифрования ящика.

Сначала без пароля проверяются все варианты сразу (параллельно): 993/SSL, 143/STARTTLS,
143 без шифрования для IMAP и 465/SSL, 587/STARTTLS, 25/STARTTLS, 587 и 25 без шифрования
для SMTP. Выбирается самый защищённый работающий вариант, и только он проверяется паролем:
так сервер не видит серию неудачных входов.
"""
from __future__ import annotations

import imaplib
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from .db import Account
from .errors import ErrorInfo, explain
from .i18n import tr

PROBE_TIMEOUT = 8

# Порядок = предпочтение: сначала шифрование с самого начала, затем STARTTLS, затем без шифрования.
IMAP_VARIANTS = [(993, "ssl"), (143, "starttls"), (143, "none")]
SMTP_VARIANTS = [(465, "ssl"), (587, "starttls"), (25, "starttls"), (587, "none"), (25, "none")]


@dataclass
class Detected:
    host: str
    port: int
    security: str


@dataclass
class AutoConfigResult:
    imap: Detected | None = None
    smtp: Detected | None = None
    imap_login: ErrorInfo | None = None   # None — вход удался или пароль не проверялся
    smtp_login: ErrorInfo | None = None
    login_checked: bool = False
    notes: list[str] = field(default_factory=list)


def candidate_hosts(email: str, kind: str, current: str = "") -> list[str]:
    domain = email.rpartition("@")[2].strip().lower()
    hosts = [current.strip()] if current.strip() else []
    if domain:
        hosts += [f"{kind}.{domain}", f"mail.{domain}", domain]
    return list(dict.fromkeys(h for h in hosts if h))


def probe_imap(host: str, port: int, security: str, timeout: float = PROBE_TIMEOUT) -> bool:
    ctx = ssl.create_default_context()
    if security == "ssl":
        im = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=timeout)
    else:
        im = imaplib.IMAP4(host, port, timeout=timeout)
    try:
        if security == "starttls":
            if "STARTTLS" not in im.capabilities:
                return False
            im.starttls(ssl_context=ctx)  # проверяет сертификат
        return True
    finally:
        try:
            im.shutdown()
        except Exception:
            pass


def probe_smtp(host: str, port: int, security: str, timeout: float = PROBE_TIMEOUT) -> bool:
    ctx = ssl.create_default_context()
    if security == "ssl":
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout)
    else:
        s = smtplib.SMTP(host, port, timeout=timeout)
    try:
        s.ehlo()
        if security == "starttls":
            if not s.has_extn("starttls"):
                return False
            s.starttls(context=ctx)
        return True
    finally:
        try:
            s.quit()
        except Exception:
            s.close()


def _best(kind: str, hosts: list[str], variants, probe, timeout) -> Detected | None:
    combos = [(host, port, security) for host in hosts for port, security in variants]

    def attempt(combo):
        try:
            return probe(*combo, timeout=timeout)
        except Exception:
            return False

    if not combos:
        return None
    with ThreadPoolExecutor(max_workers=min(16, len(combos))) as pool:
        works = list(pool.map(attempt, combos))
    # Защищённость важнее имени сервера: 993/SSL на mail.домен лучше 143 без шифрования на imap.домен.
    for port, security in variants:
        for host in hosts:
            if works[combos.index((host, port, security))]:
                return Detected(host, port, security)
    return None


def detect(acc: Account, password: str = "", timeout: float = PROBE_TIMEOUT,
           probe_imap=probe_imap, probe_smtp=probe_smtp, connect_imap=None, connect_smtp=None) -> AutoConfigResult:
    """Найти настройки ящика. Не бросает исключений: что не нашлось, остаётся None."""
    from . import protocols
    connect_imap = connect_imap or protocols.imap_connect
    connect_smtp = connect_smtp or protocols.smtp_connect
    result = AutoConfigResult()
    with ThreadPoolExecutor(max_workers=2) as pool:
        imap = pool.submit(_best, "imap", candidate_hosts(acc.email, "imap", acc.imap_host), IMAP_VARIANTS, probe_imap, timeout)
        smtp = pool.submit(_best, "smtp", candidate_hosts(acc.email, "smtp", acc.smtp_host), SMTP_VARIANTS, probe_smtp, timeout)
        result.imap, result.smtp = imap.result(), smtp.result()
    for found, name in ((result.imap, tr("приёма (IMAP)")), (result.smtp, tr("отправки (SMTP)"))):
        if found is None:
            result.notes.append(tr("Сервер {0} не найден. Укажите его вручную (адрес есть в справке почтового сервиса).", name))
        elif found.security == "none":
            result.notes.append(tr("Сервер {0} не поддерживает шифрование: пароль и письма будут передаваться открытым текстом.", name))
    if password:
        result.login_checked = True
        tested = replace(acc, username=acc.username or acc.email)
        if result.imap and result.imap.security == "none":
            result.notes.append(tr("Пароль IMAP не проверялся: сервер не поддерживает шифрование."))
        elif result.imap:
            try:
                im = connect_imap(replace(tested, imap_host=result.imap.host, imap_port=result.imap.port,
                                          imap_security=result.imap.security), password)
                try:
                    im.logout()
                except Exception:
                    pass
            except Exception as error:
                result.imap_login = explain(error)
        if result.smtp and result.smtp.security == "none":
            result.notes.append(tr("Пароль SMTP не проверялся: сервер не поддерживает шифрование."))
        elif result.smtp:
            try:
                s = connect_smtp(replace(tested, smtp_host=result.smtp.host, smtp_port=result.smtp.port,
                                         smtp_security=result.smtp.security), password)
                try:
                    s.quit()
                except Exception:
                    pass
            except Exception as error:
                result.smtp_login = explain(error)
    return result
