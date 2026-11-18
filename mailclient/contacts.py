"""Адреса для адресной книги: разбор списков адресов, проверка и запись «Имя <адрес>»."""
from __future__ import annotations

import csv
import io
import quopri
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
    name = " ".join((name or "").replace("\r", " ").replace("\n", " ").split()).strip()
    while len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
        name = name[1:-1].strip()
    if name in ("'", '"'):
        name = ""
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


def _vcard_escape(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _vcard_unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in ("n", "N") else m.group(1), value or "")


def export_contacts_csv(contacts) -> str:
    """Выгрузка списка контактов в CSV с заголовком Name,Email."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["Name", "Email"])
    for c in contacts:
        writer.writerow([getattr(c, "name", "") or "", getattr(c, "email", "") or ""])
    return buf.getvalue()


def export_contacts_vcard(contacts) -> str:
    """Выгрузка списка контактов в формат vCard 3.0 (.vcf)."""
    blocks = []
    for c in contacts:
        address = (getattr(c, "email", "") or "").strip()
        if not address:
            continue
        name = (getattr(c, "name", "") or "").strip() or address
        blocks.append(
            f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{_vcard_escape(name)}\r\n"
            f"EMAIL;TYPE=INTERNET:{address}\r\nEND:VCARD\r\n"
        )
    return "".join(blocks)


def _unfold_vcard_lines(text: str) -> list[str]:
    merged: list[str] = []
    joining_qp = False
    for line in text.splitlines():
        if joining_qp:
            if line.endswith("="):
                merged[-1] += line[:-1]
            else:
                merged[-1] += line
                joining_qp = False
            continue
        if merged and line.startswith((" ", "\t")):
            merged[-1] += line[1:]
            continue
        if ":" in line and line.endswith("=") and "QUOTED-PRINTABLE" in line.split(":", 1)[0].upper():
            merged.append(line[:-1])
            joining_qp = True
            continue
        merged.append(line)
    return merged


def _decode_vcard_value(params: list[str], value: str) -> str:
    upper = [p.upper() for p in params]
    is_qp = any("QUOTED-PRINTABLE" in p for p in upper)
    if is_qp:
        charset = "utf-8"
        for p in params:
            if p.upper().startswith("CHARSET="):
                charset = p.split("=", 1)[1].strip() or "utf-8"
                break
        try:
            return quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except LookupError:
            return quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
    return _vcard_unescape(value)


def _parse_vcard_contacts(text: str) -> list[tuple[str, str]]:
    result, seen = [], set()
    in_card = False
    fn, n_parts, emails = "", [], []

    def flush():
        nonlocal fn, n_parts, emails
        display = fn.strip()
        if not display and n_parts:
            # Формат vCard N: Фамилия;Имя;Отчество;Префикс;Суффикс
            parts = [p.strip() for p in n_parts[:3] if p.strip()]
            if len(parts) >= 2:
                display = f"{parts[1]} {parts[0]}" + (f" {parts[2]}" if len(parts) > 2 else "")
            elif parts:
                display = parts[0]
        for addr in emails:
            addr = addr.strip()
            if valid_email(addr) and addr.casefold() not in seen:
                seen.add(addr.casefold())
                result.append((clean_name(display, addr), addr))
        fn, n_parts, emails = "", [], []

    for line in _unfold_vcard_lines(text):
        stripped = line.strip()
        if stripped.upper() == "BEGIN:VCARD":
            in_card = True
            fn, n_parts, emails = "", [], []
            continue
        if stripped.upper() == "END:VCARD":
            if in_card:
                flush()
            in_card = False
            continue
        if not in_card or ":" not in line:
            continue
        prop, val = line.split(":", 1)
        segments = prop.split(";")
        name = segments[0].split(".")[-1].strip().upper()
        params = segments[1:]
        if name == "FN":
            fn = _decode_vcard_value(params, val)
        elif name == "N" and not fn:
            n_parts = [_decode_vcard_value(params, part) for part in val.split(";")]
        elif name == "EMAIL":
            decoded = _decode_vcard_value(params, val).strip()
            if decoded.lower().startswith("mailto:"):
                decoded = decoded[7:].strip()
            emails.append(decoded)
    return result


def _parse_csv_contacts(text: str) -> list[tuple[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    first = lines[0]
    delim = max((",", ";", "\t"), key=first.count)
    if first.count(delim) == 0:
        delim = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return []

    email_headers = {"email", "e-mail", "e-mail address", "email address", "mail", "адрес", "почта", "e-mail 1 - value",
                     "адрес эл. почты", "электронная почта", "адрес электронной почты"}
    name_headers = {"name", "full name", "display name", "имя", "фио", "контакт"}
    first_headers = {"first name", "given name", "имя"}
    last_headers = {"last name", "family name", "surname", "фамилия"}

    header = [col.strip().casefold() for col in rows[0]]
    has_header = any(col in email_headers or col in name_headers for col in header) and not any(
        valid_email(col) for col in rows[0]
    )
    start = 1 if has_header else 0
    email_idx = next((i for i, h in enumerate(header) if h in email_headers), None) if has_header else None
    name_idx = next((i for i, h in enumerate(header) if h in name_headers), None) if has_header else None
    first_idx = next((i for i, h in enumerate(header) if h in first_headers), None) if has_header else None
    last_idx = next((i for i, h in enumerate(header) if h in last_headers), None) if has_header else None
    if last_idx is not None and name_idx == first_idx:
        name_idx = None

    result, seen = [], set()
    for row in rows[start:]:
        if not any(cell.strip() for cell in row):
            continue
        addr = ""
        name = ""
        if email_idx is not None and email_idx < len(row):
            candidate = row[email_idx].strip()
            if valid_email(candidate):
                addr = candidate
            else:
                parsed = parse_addresses(candidate)
                if parsed:
                    name, addr = parsed[0]
        else:
            for idx, cell in enumerate(row):
                cell_s = cell.strip()
                if valid_email(cell_s):
                    addr = cell_s
                    if name_idx is None and first_idx is None and last_idx is None:
                        other = [row[j].strip() for j in range(len(row)) if j != idx and row[j].strip()]
                        name = other[0] if other else ""
                    break
                parsed = parse_addresses(cell_s)
                if parsed:
                    name, addr = parsed[0]
                    break
        if not addr or addr.casefold() in seen:
            continue
        if name_idx is not None and name_idx < len(row) and row[name_idx].strip():
            name = row[name_idx].strip()
        elif not name and (first_idx is not None or last_idx is not None):
            parts = [
                row[idx].strip()
                for idx in (first_idx, last_idx)
                if idx is not None and idx < len(row) and row[idx].strip()
            ]
            name = " ".join(parts)
        seen.add(addr.casefold())
        result.append((clean_name(name, addr), addr))
    return result


def parse_contacts_import(text: str, filename: str = "") -> list[tuple[str, str]]:
    """Разобрать импортируемые контакты из .vcf (vCard) или .csv."""
    cleaned = (text or "").lstrip("\ufeff")
    if filename.lower().endswith(".vcf") or re.search(r"(?im)^BEGIN:VCARD\b", cleaned):
        return _parse_vcard_contacts(cleaned)
    return _parse_csv_contacts(cleaned)
