"""Понятные пользователю сообщения об ошибках подключения.

Каждая ошибка превращается в ErrorInfo:
- summary — что случилось, одной фразой;
- hint    — что проверить;
- details — технический текст для кнопки «Подробнее».
"""
from __future__ import annotations

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
        return ErrorInfo("Сервер не найден", "Проверьте адрес сервера и подключение к интернету.", details)
    if isinstance(e, ConnectionRefusedError):
        return ErrorInfo("Сервер отклонил подключение", "Проверьте порт и тип шифрования.", details)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return ErrorInfo("Сервер не отвечает", "Проверьте адрес, порт и тип шифрования. "
                         "Подключение также может блокировать антивирус или брандмауэр.", details)
    if isinstance(e, ssl.SSLCertVerificationError):
        return ErrorInfo("Сертификат сервера не прошёл проверку",
                         "Проверьте, что адрес сервера указан точно так, как в сертификате.", details)
    if isinstance(e, ssl.SSLError):
        return ErrorInfo("Не удалось установить шифрованное соединение",
                         "Тип шифрования не подходит к порту. Обычно: SSL — 993/465, "
                         "STARTTLS — 143/587, без шифрования — 143/25.", details)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ErrorInfo("Сервер отправки не принял логин или пароль", AUTH_HINT, details)
    if isinstance(e, smtplib.SMTPNotSupportedError):
        return ErrorInfo("Сервер отправки не поддерживает выбранный режим",
                         "Попробуйте другой тип шифрования.", details)
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return ErrorInfo("Сервер разорвал соединение", "Проверьте порт и тип шифрования.", details)
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return ErrorInfo("Сервер не принял адрес получателя", "Проверьте адреса в полях «Кому» и «Копия».", details)
    if isinstance(e, smtplib.SMTPSenderRefused):
        return ErrorInfo("Сервер не разрешает отправку с этого адреса",
                         "Проверьте e-mail ящика и то, что включена авторизация.", details)
    if isinstance(e, smtplib.SMTPException):
        return ErrorInfo("Ошибка сервера отправки", "", details)
    if isinstance(e, imaplib.IMAP4.error):
        if "logindisabled" in low or "privacyrequired" in low:
            return ErrorInfo("Сервер не разрешает вход без шифрования",
                             "Выберите тип шифрования STARTTLS (порт 143) или SSL (порт 993).", details)
        if any(w in low for w in ("authenticationfailed", "authentication failed", "invalid credentials",
                                  "login failed", "auth", "password")):
            return ErrorInfo("Сервер не принял логин или пароль", AUTH_HINT, details)
        return ErrorInfo("Ошибка сервера приёма почты", "", details)
    if isinstance(e, UnicodeError):
        return ErrorInfo("Недопустимые символы в логине или пароле", "Проверьте раскладку клавиатуры.", details)
    if isinstance(e, OSError):
        return ErrorInfo("Сетевая ошибка", "Проверьте подключение к интернету.", details)
    return ErrorInfo("Непредвиденная ошибка", "", details)
