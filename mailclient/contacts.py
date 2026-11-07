"""Адреса для адресной книги: разбор списков адресов, проверка и запись «Имя <адрес>»."""
from __future__ import annotations

import re
from email.utils import getaddresses

# Служебные адреса (рассылки, уведомления, отказы доставки) сами в книгу не попадают:
# писать на них бессмысленно. Если пользователь сам написал на такой адрес — добавляем.
_SERVICE = re.compile(
    r"(^|[._+-])(no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon|postmaster|bounces?|notifications?)([._+-]|$)",
    re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s<>,;\"()\[\]\\]+@[^@\s<>,;\"()\[\]\\]+$")
_NEEDS_QUOTES = re.compile(r"[\"(),.:;<>@\[\]\\]")


def valid_email(address: str) -> bool:
    address = (address or "").strip()
    if not _EMAIL.match(address):
        return False
    local, domain = address.rsplit("@", 1)
    return bool(local) and bool(domain) and not domain.startswith(".") and not domain.endswith(".") and ".." not in domain


def is_service_address(address: str) -> bool:
    return bool(_SERVICE.search(address.split("@", 1)[0]))


def clean_name(name: str, address: str) -> str:
    """Имя без кавычек; адрес вместо имени («a@b.c <a@b.c>») именем не считается."""
    name = " ".join((name or "").replace("\r", " ").replace("\n", " ").split()).strip("'\" ")
    name = re.sub(r"\s+([.,])", r"\1", name)  # getaddresses: «Иван П.» без кавычек → «Иван П .»
    if not name or name.casefold() == address.casefold() or "@" in name and valid_email(name.strip("<>")):
        return ""
    return name


def parse_addresses(text: str) -> list[tuple[str, str]]:
    """«Иван <a@b.c>, d@e.f» → [(имя, адрес)]; куски без верного адреса отбрасываются."""
    result, seen = [], set()
    for name, address in getaddresses([(text or "").replace(";", ",")]):
        address = address.strip()
        if not valid_email(address) or address.casefold() in seen:
            continue
        seen.add(address.casefold())
        result.append((clean_name(name, address), address))
    return result


def raw_addresses(raw: bytes, headers=("To", "Cc", "Bcc")) -> list[tuple[str, str]]:
    """Адресаты прямо из письма (имена раскодированы, запятые в именах не ломают разбор)."""
    from .mailparse import _raw_header, _restore_8bit, decode_mime_header, parse_headers
    try:
        msg = parse_headers(raw)
        values = [_restore_8bit(v) for name in headers for v in _raw_header(msg, name, True)]
    except Exception:
        return []
    result, seen = [], set()
    for name, address in getaddresses(values):
        address = address.strip()
        if not valid_email(address) or address.casefold() in seen:
            continue
        seen.add(address.casefold())
        result.append((clean_name(decode_mime_header(name), address), address))
    return result


def format_address(name: str, address: str) -> str:
    """Запись для поля «Кому»: «Иван Петров <a@b.c>» (имя в кавычках, если в нём есть , ; и т.п.)."""
    name = clean_name(name, address)
    if not name:
        return address
    if _NEEDS_QUOTES.search(name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{address}>"
