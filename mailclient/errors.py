"""Понятные пользователю сообщения об ошибках подключения.

Каждая ошибка превращается в ErrorInfo:
- summary — что случилось, одной фразой;
- hint    — что проверить;
- details — технический текст для кнопки «Подробнее».
"""
from __future__ import annotations

from .i18n import tr

import imaplib
import smtplib
import socket
import ssl
from dataclasses import dataclass


class MailOperationError(RuntimeError):
    """Ожидаемый отказ операции, текст которого можно показать пользователю."""


@dataclass
class ErrorInfo:
    summary: str
    hint: str
    details: str

    def short(self) -> str:
        return f"{self.summary}. {self.hint}" if self.hint else self.summary


def server_text(data) -> str:
    """Ответ сервера imaplib ([b'...'], bytes, str) — в обычный текст без b'...'."""
    if isinstance(data, (list, tuple)):
        return " ".join(filter(None, (server_text(item) for item in data))).strip()
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace").strip()
    return "" if data is None else str(data).strip()


def _text(e: Exception) -> str:
    """Текст исключения без b'...' от imaplib/smtplib, с кодом ответа SMTP."""
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "; ".join(f"{addr}: {code} {server_text(msg)}" for addr, (code, msg) in e.recipients.items()) \
            or type(e).__name__
    if isinstance(e, smtplib.SMTPResponseException):
        return f"{e.smtp_code} {server_text(e.smtp_error)}".strip()
    parts = [server_text(a) for a in (getattr(e, "args", ()) or (str(e),))]
    return " ".join(filter(None, parts)).strip() or type(e).__name__


def smtp_code(e: Exception) -> int | None:
    """Код ответа SMTP (для отказа всем адресатам — наибольший)."""
    if isinstance(e, smtplib.SMTPRecipientsRefused) and e.recipients:
        return max(code for code, _ in e.recipients.values())
    return getattr(e, "smtp_code", None)


def describe(e: Exception) -> str:
    """Одна строка для списка писем: понятное описание и ответ сервера."""
    info = explain(e)
    server = _text(e)
    return info.short() if not server or server in info.short() else tr("{0} Ответ сервера: {1}", info.short(), server)


GOOGLE_APP_PASSWORDS = "https://myaccount.google.com/apppasswords"

AUTH_HINT = ("Проверьте логин и пароль. Обычно логин — это полный адрес почты. "
             "Если в почте включена двухфакторная защита, нужен пароль приложения.")


def explain(e: Exception) -> ErrorInfo:
    kind = "IMAP" if isinstance(e, imaplib.IMAP4.error) else type(e).__name__
    details = f"{kind}: {_text(e)}"
    low = _text(e).lower()

    if isinstance(e, MailOperationError):
        return ErrorInfo(_text(e), "", details)
    # Gmail: «[ALERT] Application-specific password required», SMTP 534 5.7.9, WEBALERT.
    if any(w in low for w in ("application-specific password", "5.7.9", "webloginrequired", "[webalert",
                              "please log in via your web browser")):
        return ErrorInfo(tr("Google не принимает обычный пароль"),
                         tr("Выберите способ входа «Google» в настройках ящика или создайте пароль приложения: {0}",
                            GOOGLE_APP_PASSWORDS), details)
    if isinstance(e, socket.gaierror):
        return ErrorInfo(tr("Сервер не найден"), tr("Проверьте адрес сервера и подключение к интернету."), details)
    if isinstance(e, ConnectionRefusedError):
        return ErrorInfo(tr("Сервер отклонил подключение"), tr("Проверьте порт и тип шифрования."), details)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return ErrorInfo(tr("Сервер не отвечает"), tr("Проверьте адрес, порт и тип шифрования. Подключение также может блокировать антивирус или брандмауэр."), details)
    if isinstance(e, ssl.SSLCertVerificationError):
        return ErrorInfo(tr("Сертификат сервера не прошёл проверку"),
                         tr("Проверьте, что адрес сервера указан точно так, как в сертификате."), details)
    if isinstance(e, ssl.SSLError):
        return ErrorInfo(tr("Не удалось установить шифрованное соединение"),
                         tr("Тип шифрования не подходит к порту. Обычно: SSL — 993/465, STARTTLS — 143/587, без шифрования — 143/25."), details)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ErrorInfo(tr("Сервер отправки не принял логин или пароль"), tr(AUTH_HINT), details)
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return ErrorInfo(tr("Сервер отправки не поддерживает выбранный режим"),
                         tr("Попробуйте другой тип шифрования."), details)
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return ErrorInfo(tr("Сервер разорвал соединение"), tr("Проверьте порт и тип шифрования."), details)
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return ErrorInfo(tr("Сервер не принял адрес получателя"), tr("Проверьте адреса в полях «Кому» и «Копия»."), details)
    if isinstance(e, smtplib.SMTPSenderRefused):
        return ErrorInfo(tr("Сервер не разрешает отправку с этого адреса"),
                         tr("Проверьте e-mail ящика и то, что включена авторизация."), details)
    if isinstance(e, smtplib.SMTPDataError):
        return ErrorInfo(tr("Сервер отправки отклонил письмо"), tr("Ответ сервера: {0}", _text(e)), details)
    if isinstance(e, smtplib.SMTPException):
        return ErrorInfo(tr("Ошибка сервера отправки"), tr("Ответ сервера: {0}", _text(e)), details)
    if isinstance(e, imaplib.IMAP4.abort):
        return ErrorInfo(tr("Связь с сервером приёма почты прервалась"),
                         tr("Проверьте подключение к интернету. Проверка почты повторится автоматически."), details)
    if isinstance(e, imaplib.IMAP4.error):
        if "logindisabled" in low or "privacyrequired" in low:
            return ErrorInfo(tr("Сервер не разрешает вход без шифрования"),
                             tr("Выберите тип шифрования STARTTLS (порт 143) или SSL (порт 993)."), details)
        if any(w in low for w in ("authenticationfailed", "authentication failed", "invalid credentials",
                                  "login failed", "[auth]", "authorizationfailed", "incorrect password",
                                  "invalid password", "bad password", "logon failure")):
            return ErrorInfo(tr("Сервер не принял логин или пароль"), tr(AUTH_HINT), details)
        if "overquota" in low or "quota" in low:
            return ErrorInfo(tr("Ящик на сервере переполнен"), tr("Удалите ненужные письма или очистите корзину."), details)
        if "nonexistent" in low or "mailbox doesn't exist" in low or "no such mailbox" in low:
            return ErrorInfo(tr("Папка не найдена на сервере"), tr("Возможно, её удалили или переименовали в другой программе."), details)
        return ErrorInfo(tr("Сервер приёма почты отклонил команду"), tr("Ответ сервера: {0}", _text(e)), details)
    if isinstance(e, UnicodeError):
        return ErrorInfo(tr("Недопустимые символы в логине или пароле"), tr("Проверьте раскладку клавиатуры."), details)
    if isinstance(e, OSError):
        return ErrorInfo(tr("Сетевая ошибка"), tr("Проверьте подключение к интернету."), details)
    return ErrorInfo(tr("Непредвиденная ошибка"), tr("Подробности записаны в журнал: Справка → Журнал ошибок."), details)
