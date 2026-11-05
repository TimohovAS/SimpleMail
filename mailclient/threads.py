"""Переписка (цепочка писем): ключи для поиска связанных писем.

Письма связываются так же, как в других почтовых программах:
- по Message-ID и заголовкам In-Reply-To / References (основной способ);
- запасной вариант — по теме без «Re:», «Fwd:» и т.п., если почтовая программа
  собеседника не ставит ссылки на предыдущие письма (см. Database.thread).
"""
from __future__ import annotations

import email
import re
from email import policy

_ID_RE = re.compile(r"<([^<>\s]+)>")
# Re:, RE[2]:, Fwd:, Fw:, AW:, WG:, SV:, VS:, Odg:, Ответ:, Отв:, Пересл: … — в любом количестве и порядке
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|aw|wg|sv|vs|tr|odg|odgovor|ответ|отв|пересл|пересылка|ha)\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*)+",
    re.IGNORECASE)
MAX_HEADER_BYTES = 256 * 1024


def msgid_key(value: str | None) -> str:
    """Message-ID в виде ключа: без угловых скобок, в нижнем регистре."""
    value = (value or "").strip()
    match = _ID_RE.search(value)
    return (match.group(1) if match else value.strip("<> \t\r\n")).lower()


def ref_keys(in_reply_to: str | None, references: str | None) -> list[str]:
    """Ключи писем, на которые отвечает письмо (References по порядку, затем In-Reply-To)."""
    keys: list[str] = []
    for value in (references or "", in_reply_to or ""):
        found = _ID_RE.findall(value)
        if not found and value.strip() and " " not in value.strip():
            found = [value.strip()]
        for item in found:
            key = item.strip("<> \t\r\n").lower()
            if key and key not in keys:
                keys.append(key)
    return keys


def has_reply_prefix(subject: str | None) -> bool:
    return bool(_PREFIX_RE.match(subject or ""))


def subject_key(subject: str | None) -> str:
    """Тема для сравнения: без Re:/Fwd: и лишних пробелов, без учёта регистра."""
    text = _PREFIX_RE.sub("", subject or "")
    return " ".join(text.split()).casefold()


def header_bytes(raw: bytes) -> bytes:
    """Только заголовки письма (до первой пустой строки)."""
    head = raw[:MAX_HEADER_BYTES]
    for sep in (b"\r\n\r\n", b"\n\n"):
        pos = head.find(sep)
        if pos >= 0:
            return head[:pos + len(sep)]
    return head


def read_header_bytes(path: str | None) -> bytes:
    """Заголовки письма из файла .eml, не читая вложения."""
    if not path:
        return b""
    try:
        with open(path, "rb") as f:
            data = b""
            while len(data) < MAX_HEADER_BYTES:
                chunk = f.read(16384)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data or b"\n\n" in data:
                    break
        return header_bytes(data)
    except OSError:
        return b""


def raw_refs(raw: bytes) -> list[str]:
    """Ключи In-Reply-To/References из сырого письма (или только его заголовков)."""
    if not raw:
        return []
    msg = email.message_from_bytes(header_bytes(raw), policy=policy.compat32)
    return ref_keys(str(msg.get("In-Reply-To") or ""), " ".join(str(v) for v in msg.get_all("References") or []))
