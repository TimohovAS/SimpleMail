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


@dataclass
class ErrorInfo:
    summary: str
    hint: str
    details: str

    def short(self) -> str:
        return f"{self.summary}. {self.hint}" if self.hint else self.summary


def _text(e: Exception) -> str:
    """Текст исключения без b'...' от imaplib/smtplib."""
    parts = []
    for a in getattr(e, "args", ()) or (str(e),):
        if isinstance(a, bytes):
            a = a.decode("utf-8", "replace")
        parts.append(str(a))
    return " ".join(parts).strip() or type(e).__name__


AUTH_HINT = ("Проверьте логин и пароль. Обычно логин — это полный адрес почты. "
             "Если в почте включена двухфакторная защита, нужен пароль приложения.")


def explain(e: Exception) -> ErrorInfo:
    kind = "IMAP" if isinstance(e, imaplib.IMAP4.error) else type(e).__name__
    details = f"{kind}: {_text(e)}"
    low = _text(e).lower()

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
    if isinstance(e, smtplib.SMTPException):
        return ErrorInfo(tr("Ошибка сервера отправки"), "", details)
    if isinstance(e, imaplib.IMAP4.error):
        if "logindisabled" in low or "privacyrequired" in low:
            return ErrorInfo(tr("Сервер не разрешает вход без шифрования"),
                             tr("Выберите тип шифрования STARTTLS (порт 143) или SSL (порт 993)."), details)
        if any(w in low for w in ("authenticationfailed", "authentication failed", "invalid credentials",
                                  "login failed", "auth", "password")):
            return ErrorInfo(tr("Сервер не принял логин или пароль"), tr(AUTH_HINT), details)
        return ErrorInfo(tr("Ошибка сервера приёма почты"), "", details)
    if isinstance(e, UnicodeError):
        return ErrorInfo(tr("Недопустимые символы в логине или пароле"), tr("Проверьте раскладку клавиатуры."), details)
    if isinstance(e, OSError):
        return ErrorInfo(tr("Сетевая ошибка"), tr("Проверьте подключение к интернету."), details)
    return ErrorInfo(tr("Непредвиденная ошибка"), "", details)
