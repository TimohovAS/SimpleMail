"""Разбор писем (RFC 822) в поля для базы."""
from __future__ import annotations

import email
import html
import mimetypes
import re
from email import policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, getaddresses

from .db import Message


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        for cs in (charset, "utf-8", "cp1251", "koi8-r", "latin-1"):
            try:
                return payload.decode(cs)
            except Exception:
                continue
        return payload.decode("utf-8", "replace")


def html_to_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style).*?</\1>", " ", src)
    src = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    src = html.unescape(src)
    src = re.sub(r"[ \t\r\f\v]+", " ", src)
    return re.sub(r"\n\s*\n+", "\n\n", src).strip()


def parse_message(raw: bytes) -> Message:
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            text_parts.append(_part_text(part))
        elif ctype == "text/html":
            html_parts.append(_part_text(part))

    body_html = "\n".join(html_parts)
    body_text = "\n".join(text_parts) or (html_to_text(body_html) if body_html else "")

    try:
        date_ts = int(parsedate_to_datetime(msg.get("Date")).timestamp())
    except Exception:
        date_ts = 0

    recipients = ", ".join(
        decode_mime_header(a) if not n else f"{decode_mime_header(n)} <{a}>"
        for n, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))
    )

    return Message(
        message_id=(msg.get("Message-ID") or "").strip(),
        subject=decode_mime_header(msg.get("Subject")),
        sender=decode_mime_header(msg.get("From")),
        recipients=recipients,
        date_ts=date_ts,
        body_text=body_text,
        body_html=body_html,
        raw=raw,
    )


def header(raw: bytes, name: str) -> str:
    """Значение заголовка из сырого письма (например, X-Spam-Flag)."""
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    return decode_mime_header(msg.get(name))


def attachments(raw: bytes) -> list[tuple[str, bytes]]:
    """Настоящие вложения письма.

    Картинки, которые показываются внутри текста (логотипы и подписи вида image001.png,
    на них ссылается HTML через cid:), вложениями не считаются.
    """
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    html_text = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment":
            html_text += _part_text(part)
    used_cids = {c.lower() for c in re.findall(r"cid:([^\"'\s>)]+)", html_text, re.I)}
    def candidates(part):
        # Вложенное письмо — один файл .eml, а не набор его внутренних частей.
        if part.get_filename() or part.get_content_disposition() == "attachment":
            yield part
        elif part.is_multipart():
            for child in part.get_payload():
                yield from candidates(child)

    result = []
    for part in candidates(msg):
        fn = part.get_filename()
        if not fn:
            extension = mimetypes.guess_extension(part.get_content_type()) or ".bin"
            fn = f"attachment-{len(result) + 1}{extension}"
        cid = (part.get("Content-ID") or "").strip().strip("<>").lower()
        if cid and (cid in used_cids or part.get_content_disposition() == "inline"):
            continue
        if part.get_content_type() == "message/rfc822" and part.is_multipart():
            data = b"\r\n".join(child.as_bytes() for child in part.get_payload())
        elif part.is_multipart():
            data = part.as_bytes()
        else:
            data = part.get_payload(decode=True) or b""
        result.append((decode_mime_header(fn), data))
    return result
