"""Разбор писем (RFC 822) в поля для базы."""
from __future__ import annotations

import codecs
import email
import html
import mimetypes
import re
from email import policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, getaddresses

from .db import Message


def _is_cyrillic(ch: str) -> bool:
    return "\u0400" <= ch <= "\u04ff"


def _cyrillic_score(text: str) -> int:
    """Русский текст в основном строчный: при чтении koi8-r как cp1251 (и наоборот)
    регистр букв переворачивается."""
    return sum(1 if ch.islower() else -1 if ch.isupper() else 0
               for ch in text if _is_cyrillic(ch))


# Буквы сербской (хорватской, боснийской) латиницы, которых нет в ASCII.
_SOUTH_SLAVIC = frozenset("čćđšžČĆĐŠŽ")
# Те же байты в windows-1252 / iso-8859-1: č→è, ć→æ, đ→ð и заглавные.
_LATIN1_LOOKALIKES = frozenset("èæðÈÆÐ")


def _south_slavic(text: str) -> bool:
    """Все не-ASCII буквы — буквы сербской латиницы (и хотя бы одна есть)."""
    letters = [ch for ch in text if not ch.isascii() and ch.isalpha()]
    return bool(letters) and all(ch in _SOUTH_SLAVIC for ch in letters)


def _mixed_script(text: str) -> bool:
    """Есть слово, где латиница перемешана с кириллицей («raиun») — признак неверной кодировки."""
    for word in re.findall(r"[^\W\d_]+", text):
        if any(_is_cyrillic(ch) for ch in word) and any(ch.isascii() for ch in word):
            return True
    return False


def _try_cp1250(data: bytes) -> str | None:
    try:
        return data.decode("cp1250")
    except UnicodeError:
        return None


def _fix_western(data: bytes, text: str) -> str:
    """Сербские письма часто объявляют iso-8859-1 / windows-1252, а пишут в windows-1250:
    «raèun» вместо «račun», «Po\x9atovani» вместо «Poštovani». Переходим на windows-1250,
    только если в нём все не-ASCII буквы — сербские, а в объявленной кодировке видны
    управляющие символы или è/æ/ð."""
    if text.isascii():
        return text
    suspicious = any(ch in _LATIN1_LOOKALIKES or "\x80" <= ch <= "\x9f" for ch in text)
    if not suspicious:
        return text
    alt = _try_cp1250(data)
    return alt if alt is not None and alt != text and _south_slavic(alt) else text


_WESTERN = frozenset(("iso8859-1", "cp1252"))


def _codec_name(charset: str | None) -> str | None:
    try:
        return codecs.lookup(charset).name if charset else None
    except LookupError:
        return None


def _decode_bytes(data: bytes, charset: str | None = None) -> str:
    """Байты заголовка в текст: заявленная кодировка, затем UTF-8, затем cp1251/koi8-r
    или windows-1250 (сербская латиница)."""
    if charset and charset.lower() not in ("unknown-8bit", "us-ascii", "ascii"):
        try:
            text = data.decode(charset)
        except (LookupError, UnicodeError):
            pass
        else:
            return _fix_western(data, text) if _codec_name(charset) in _WESTERN else text
    try:
        return data.decode("utf-8")
    except UnicodeError:
        pass
    candidates = []
    for cs in ("cp1251", "koi8-r"):
        try:
            candidates.append(data.decode(cs))
        except UnicodeError:
            continue
    cyrillic = max(candidates, key=_cyrillic_score) if candidates else None
    # Латиница с одной «русской» буквой внутри слова («raиun») — на самом деле windows-1250.
    if cyrillic is None or _mixed_script(cyrillic):
        latin = _try_cp1250(data)
        if latin is not None and (cyrillic is None or _south_slavic(latin)):
            return latin
    if cyrillic is not None:
        return cyrillic
    return data.decode("latin-1")


def _restore_8bit(value: str) -> str:
    """Письмо без MIME-кодирования заголовков (8-битный текст) разбирается Python
    в строку с суррогатами; восстанавливаем байты и подбираем кодировку."""
    if not any("\udc80" <= ch <= "\udcff" for ch in value):
        return value
    return _decode_bytes(value.encode("utf-8", "surrogateescape"))


def decode_mime_header(value) -> str:
    """Заголовок письма в читаемый текст: RFC 2047 (=?..?=) и 8-битный текст без кодирования."""
    if not value:
        return ""
    try:
        if isinstance(value, str):
            value = _restore_8bit(value)
        chunks = []
        for chunk, charset in decode_header(value):
            if isinstance(chunk, bytes):
                chunk = _decode_bytes(chunk, charset)
            # charset None — незакодированный кусок: make_header не добавит к нему лишних пробелов
            chunks.append((chunk, "utf-8" if charset else None))
        return str(make_header(chunks))
    except Exception:
        return _restore_8bit(str(value))


def _raw_header(msg, name: str, all_values: bool = False):
    """Исходное значение заголовка (без замены 8-битных символов на «�»)."""
    values = [v for k, v in msg.raw_items() if k.lower() == name.lower()]
    return values if all_values else (values[0] if values else None)


def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        for cs in (charset, "utf-8", "cp1251", "koi8-r", "latin-1"):
            try:
                text = payload.decode(cs)
            except Exception:
                continue
            return _fix_western(payload, text) if _codec_name(cs) in _WESTERN else text
        return payload.decode("utf-8", "replace")


def html_to_text(src: str) -> str:
    src = re.sub(r"(?is)<(script|style).*?</\1>", " ", src)
    src = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", src)
    src = re.sub(r"<[^>]+>", " ", src)
    src = html.unescape(src)
    src = re.sub(r"[ \t\r\f\v]+", " ", src)
    return re.sub(r"\n\s*\n+", "\n\n", src).strip()


_NAME_RE = re.compile(r'(?:file)?name\*?\s*=\s*(?:"((?:\\.|[^"])*)"|([^;\s]+))', re.I)


def _filename(part) -> str | None:
    """Имя вложения; 8-битные имена без кодирования восстанавливаем из исходного заголовка."""
    fn = part.get_filename()
    if fn and "\ufffd" not in fn and not any("\udc80" <= ch <= "\udcff" for ch in fn):
        return fn
    for header in ("Content-Disposition", "Content-Type"):
        raw = _raw_header(part, header)
        if isinstance(raw, str) and (match := _NAME_RE.search(raw)):
            return _restore_8bit(match.group(1) if match.group(1) is not None else match.group(2))
    return fn


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
        for n, a in getaddresses([_restore_8bit(v) for v in _raw_header(msg, "To", True) + _raw_header(msg, "Cc", True)])
    )

    from .threads import msgid_key, ref_keys, subject_key
    message_id = (msg.get("Message-ID") or "").strip()
    subject = decode_mime_header(_raw_header(msg, "Subject"))
    return Message(
        message_id=message_id,
        msgid_key=msgid_key(message_id),
        subject_key=subject_key(subject),
        refs=" ".join(ref_keys(str(msg.get("In-Reply-To") or ""),
                               " ".join(str(v) for v in msg.get_all("References") or []))),
        subject=subject,
        sender=decode_mime_header(_raw_header(msg, "From")),
        recipients=recipients,
        date_ts=date_ts,
        body_text=body_text,
        body_html=body_html,
        raw=raw,
    )


def header(raw: bytes, name: str) -> str:
    """Значение заголовка из сырого письма (например, X-Spam-Flag)."""
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    return decode_mime_header(_raw_header(msg, name))


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
        if _filename(part) or part.get_content_disposition() == "attachment":
            yield part
        elif part.is_multipart():
            for child in part.get_payload():
                yield from candidates(child)

    result = []
    for part in candidates(msg):
        fn = _filename(part)
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
