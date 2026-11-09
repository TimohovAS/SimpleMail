"""Просмотр письма.

HTML-письма показываются встроенным браузером (Qt WebEngine): он рисует письмо
в отдельном процессе, поэтому тяжёлые рассылки не подвешивают программу.

Безопасность и приватность:
- JavaScript выключен;
- картинки и прочее содержимое из интернета НЕ загружаются, пока пользователь не нажмёт
  «Показать картинки» (иначе отправитель узнаёт, что письмо открыто);
- ссылки открываются во внешнем браузере;
- встроенные картинки (cid:) берутся из самого письма.
"""
from __future__ import annotations

from ..i18n import tr

import email
import html
import json
import logging
import os
import time
import re
import base64
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote_to_bytes
from email import policy

from PySide6.QtCore import QBuffer, QByteArray, QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QStackedLayout, QTextBrowser, QVBoxLayout, QWidget,
)

from .theme import is_dark
from .image_resources import raster_image

try:
    from PySide6.QtWebEngineCore import (
        QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings,
        QWebEngineUrlRequestInterceptor, QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except Exception:  # WebEngine не установлен — работаем через простой просмотрщик
    HAS_WEBENGINE = False

SCHEME = b"simplemail"
URL_RE = re.compile(r"(https?://[^\s<>\"']+)")

CSS_REMOTE_RE = re.compile(r"(?:url\(\s*[\"']?\s*|@import\s*[\"']\s*)(?:https?:)?//", re.IGNORECASE)


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
             "track", "wbr"}
QUOTE_OPEN = "<details class=\"quote\"><summary title=\"{0}\">•••</summary>"
# Outlook: «От: … Отправлено: …» над цитатой — граница сверху у блока с заголовками
OUTLOOK_SEPARATOR_RE = re.compile(r"border-top\s*:\s*solid[^;]*;\s*padding\s*:\s*3\.0pt\s+0(?:cm|in)\s+0(?:cm|in)\s+0(?:cm|in)",
                                  re.IGNORECASE)


def _quote_kind(tag: str, attrs) -> str | None:
    """elem — свернуть сам элемент (цитата), rest — всё от него до конца письма (Outlook)."""
    a = {k: (v or "") for k, v in attrs}
    classes = a.get("class", "").lower().split()
    if tag == "blockquote" or (tag == "div" and ({"gmail_quote", "yahoo_quoted"} & set(classes))):
        return "elem"
    if tag in ("div", "hr") and a.get("id", "").lower() in ("divrplyfwdmsg", "appendonsend", "stopspelling"):
        return "rest"
    if tag == "div" and OUTLOOK_SEPARATOR_RE.search(a.get("style", "")):
        return "rest"
    return None


class _MailHTML(HTMLParser):
    DROP_CONTENT = {"script", "object", "embed", "iframe", "frame", "frameset", "applet"}
    DROP_TAG = {"base", "meta", "link", "form", "input", "button"}
    FRAGMENT_DROP = {"html", "head", "body"}

    def __init__(self, local_images_only=False, fragment=False, fold_quotes=False):
        """fragment — письмо внутри страницы переписки: без html/head/body/title, стили
        собираются отдельно (self.styles), открытые теги закрываются в finish();
        fold_quotes — цитаты предыдущих писем сворачиваются (<details>)."""
        super().__init__(convert_charrefs=False)
        self.local_images_only = local_images_only
        self.fragment, self.fold_quotes = fragment, fold_quotes
        self.in_style = False
        self.output, self.blocked = [], []
        self.styles: list[str] = []
        self.remote = False
        self.stack: list[str] = []   # открытые элементы (для сворачивания цитат и закрытия фрагмента)
        self.fold = None             # (глубина, вид) — открытый <details> цитаты
        self.closers: list[str] = []  # предки свёрнутого «до конца письма» блока: закрываются в конце

    def handle_starttag(self, tag, attrs):
        if tag == "style" and not self.blocked:
            self.in_style = True
        for key, value in attrs:
            if value and ((key in {"src", "background", "srcset", "data"} or
                           (key in {"href", "xlink:href"} and tag in {"link", "base", "image", "use"})) and
                          re.search(r"(?:https?:)?//", value, re.IGNORECASE)):
                self.remote = True
            if key == "style" and value and CSS_REMOTE_RE.search(value):
                self.remote = True
        if tag in self.DROP_CONTENT or (self.local_images_only and tag == "svg") or (self.fragment and tag == "title"):
            if tag not in {"embed", "frame"}:
                self.blocked.append(tag)
            return
        if self.fragment and (tag in self.FRAGMENT_DROP or tag == "style"):
            return
        if not self.blocked and tag not in self.DROP_TAG:
            if self.local_images_only:
                source = (dict(attrs).get("src") or "").lower()
                if tag == "img" and not (source.startswith("cid:") or re.match(r"data:image/(?:png|jpeg|gif|bmp|webp|tiff|x-icon)[;,]", source)):
                    self.output.append(html.escape(dict(attrs).get("alt") or ""))
                    return
                attrs = [(key, self._local_style(value or "") if key == "style" else value)
                         for key, value in attrs if key not in {"background", "srcset"}]
                text = "<" + tag + "".join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in attrs) + ">"
            else:
                text = self.get_starttag_text()
            self._open(tag, attrs, text)

    def _open(self, tag, attrs, text):
        if self.fold_quotes and self.fold is None:
            kind = _quote_kind(tag, attrs)
            if kind:
                self.output.append(QUOTE_OPEN.format(html.escape(tr("Показать или скрыть цитату"), quote=True)))
                self.fold = (len(self.stack), kind)
        self.output.append(text)
        if self.fragment and tag not in VOID_TAGS:
            self.stack.append(tag)
        if self.fold and self.fold[0] == len(self.stack) and self.fold[1] == "elem":
            self._end_fold()  # пустой элемент (void): свернуть нечего

    def _end_fold(self):
        depth, _ = self.fold
        self.output.extend(f"</{t}>" for t in reversed(self.stack[depth:]))
        del self.stack[depth:]
        self.output.append("</details>")
        self.output.extend(self.closers)
        self.closers = []
        self.fold = None

    def _close(self, tag):
        if not self.fragment:
            self.output.append(f"</{tag}>")
            return
        if tag not in self.stack:
            return  # лишний закрывающий тег — во фрагменте мог бы закрыть обёртку письма
        idx = len(self.stack) - 1 - self.stack[::-1].index(tag)
        if self.fold:
            depth, kind = self.fold
            if kind == "elem" and idx < depth:
                self._end_fold()  # закрывается предок раньше самой цитаты (кривая разметка)
            elif kind == "elem" and idx == depth:
                self.output.extend(f"</{t}>" for t in reversed(self.stack[idx + 1:]))
                self.output.append(f"</{tag}>")
                del self.stack[idx:]
                self.output.append("</details>")
                self.fold = None
                return
            if kind == "rest" and idx < depth:
                # Закрывается предок блока «до конца письма»: не закрываем его, иначе остаток
                # письма (сама цитата) окажется вне свёрнутого блока; закроем в finish().
                self.output.extend(f"</{t}>" for t in reversed(self.stack[depth:]))
                self.closers += [f"</{t}>" for t in reversed(self.stack[idx:depth])]
                del self.stack[idx:]
                self.fold = (idx, kind)
                return
        self.output.append(f"</{tag}>")
        del self.stack[idx:]

    def finish(self) -> str:
        """Закрыть всё открытое (письмо не должно «съесть» следующие письма переписки)."""
        self.close()
        if self.fold:
            self._end_fold()
        self.output.extend(f"</{t}>" for t in reversed(self.stack))
        self.stack = []
        return "".join(self.output)

    @staticmethod
    def _local_style(data):
        return re.sub(r"url\([^)]*\)|@import[^;]*;?", "", data, flags=re.IGNORECASE)

    def handle_startendtag(self, tag, attrs):
        previous = len(self.blocked)
        self.handle_starttag(tag, attrs)
        del self.blocked[previous:]
        if self.fragment and tag not in VOID_TAGS and self.stack and self.stack[-1] == tag:
            self._close(tag)  # <div/> и т.п.

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if self.blocked:
            if self.blocked[-1] == tag:
                self.blocked.pop()
        elif self.fragment and (tag in self.FRAGMENT_DROP or tag == "style"):
            return
        elif tag not in self.DROP_CONTENT | self.DROP_TAG:
            self._close(tag)

    def handle_data(self, data):
        if CSS_REMOTE_RE.search(data):
            self.remote = True
        if not self.blocked:
            if self.fragment and self.in_style:
                self.styles.append(self._local_style(data) if self.local_images_only else data)
            else:
                self.output.append(self._local_style(data) if self.local_images_only and self.in_style else data)

    def handle_entityref(self, name):
        if not self.blocked:
            self.output.append(f"&{name};")

    def handle_charref(self, name):
        if not self.blocked:
            self.output.append(f"&#{name};")


class _FirstPaint(QWidget):
    """Пустая область письма до создания встроенного браузера; сообщает о первой отрисовке."""
    def __init__(self, on_painted):
        super().__init__()
        self._on_painted = on_painted
        self.setBackgroundRole(QPalette.Base)
        self.setAutoFillBackground(True)

    def paintEvent(self, event):
        super().paintEvent(event)
        callback, self._on_painted = self._on_painted, None
        if callback is not None:
            callback()


class _SafeTextBrowser(QTextBrowser):
    IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp", "image/tiff", "image/x-icon"}

    def __init__(self, view):
        super().__init__()
        self.view = view

    def loadResource(self, kind, url):
        from PySide6.QtGui import QTextDocument
        if kind != QTextDocument.ImageResource:
            return None
        if url.scheme() == "cid":
            mime, data = self.view._inline.get(url.path(), ("", b""))
            return raster_image(mime, data)
        if url.scheme() == "data":
            metadata, separator, payload = url.toString().partition(",")
            if separator and metadata[5:].split(";")[0].lower() in self.IMAGE_TYPES:
                try:
                    data = unquote_to_bytes(payload)
                    return raster_image(metadata[5:].split(";")[0], base64.b64decode(data, validate=True) if metadata.lower().endswith(";base64") else data)
                except ValueError:
                    pass
        # Включая file:, UNC, qrc: и HTTP: файлы с диска и сеть не читаем.
        return None


SAFE_LINK_SCHEMES = ("http", "https")


def link_action(url: QUrl) -> str:
    """Что делать со ссылкой из письма: open — внешний браузер, mailto — своё окно письма,
    block — не открывать (file:, ms-msdt:, search-ms: и т.п. могут запустить программу)."""
    scheme = url.scheme().lower()
    if scheme in SAFE_LINK_SCHEMES and url.host():
        return "open"
    if scheme == "mailto":
        return "mailto"
    return "block"


def parse_mailto(url: QUrl) -> dict:
    """mailto:a@b?subject=..&cc=..&body=.. -> параметры окна письма."""
    from urllib.parse import parse_qs, unquote, urlsplit
    parts = urlsplit(url.toString(QUrl.ComponentFormattingOption.FullyEncoded))
    query = {k.lower(): ", ".join(v) for k, v in parse_qs(parts.query).items()}
    to = ", ".join(filter(None, [unquote(parts.path), query.get("to", "")]))
    return {key: value for key, value in (("to", to), ("cc", query.get("cc", "")),
                                          ("subject", query.get("subject", "")),
                                          ("body", query.get("body", ""))) if value}


def register_schemes() -> None:
    """Вызывать ДО создания QApplication."""
    if not HAS_WEBENGINE:
        return
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    for name in (SCHEME, b"cid"):
        s = QWebEngineUrlScheme(name)
        s.setSyntax(QWebEngineUrlScheme.Syntax.Path)
        s.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalScheme
                   | QWebEngineUrlScheme.Flag.LocalAccessAllowed)
        QWebEngineUrlScheme.registerScheme(s)


def has_remote_content(body_html: str) -> bool:
    parser = _MailHTML()
    parser.feed(body_html or "")
    return parser.remote


def inline_parts(raw: bytes) -> dict[str, tuple[str, bytes]]:
    """Встроенные картинки письма: Content-ID -> (тип, данные)."""
    result = {}
    if not raw:
        return result
    msg = email.message_from_bytes(raw, policy=policy.compat32)
    for part in msg.walk():
        cid = part.get("Content-ID")
        if cid and not part.is_multipart():
            result[cid.strip().strip("<>")] = (part.get_content_type(), part.get_payload(decode=True) or b"")
    return result


def message_style() -> str:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    palette = app.palette() if app else QPalette()
    return (f"body{{color:{palette.color(QPalette.Text).name()};"
            f"background-color:{palette.color(QPalette.Base).name()};}}"
            f"a{{color:{palette.color(QPalette.Link).name()};}}"
            f".q{{color:{palette.color(QPalette.PlaceholderText).name()};}}")


def text_to_html(text: str, *, style=None) -> str:
    lines = []
    for line in html.escape(text or "").splitlines():
        line = URL_RE.sub(r'<a href="\1">\1</a>', line)
        if line.startswith("&gt;"):
            line = f'<span class="q">{line}</span>'
        lines.append(line)
    return ("<html><head><meta charset='utf-8'><style>"
            "body{font-family:'Segoe UI',sans-serif;font-size:10pt;white-space:pre-wrap;margin:8px;}"
            + (message_style() if style is None else style) + "</style></head><body>" + "\n".join(lines) + "</body></html>")


def sanitize_html(body: str, *, local_images_only=False) -> str:
    parser = _MailHTML(local_images_only=local_images_only)
    parser.feed(body)
    parser.close()
    return "".join(parser.output)


def wrap_html(body: str) -> str:
    return "<meta charset='utf-8'><style>" + message_style() + "</style>" + sanitize_html(body)


def prepare_message(m, style):
    """Read-only MIME/HTML preparation; style is captured on the GUI thread."""
    if m.body_html:
        parser = _MailHTML()
        parser.feed(m.body_html)
        parser.close()
        page = "<meta charset='utf-8'><style>" + style + "</style>" + "".join(parser.output)
        inline = inline_parts(m.raw) if "cid:" in m.body_html.lower() else {}
        return page, inline, parser.remote
    return text_to_html(m.body_text, style=style), {}, False


# ---------- переписка: все письма цепочки на одной странице ----------
TEXT_QUOTE_HEADER_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:original message|исходное сообщение|оригинальное сообщение|izvorna poruka|originalna poruka)"
    r"\s*-{2,}|_{20,})\s*$", re.IGNORECASE)
TEXT_FROM_RE = re.compile(r"^\s*\*?(?:from|от|od)\s*:\*?\s", re.IGNORECASE)
TEXT_SENT_RE = re.compile(r"^\s*\*?(?:sent|date|отправлено|дата|poslato|poslano|datum)\s*:", re.IGNORECASE)
MIN_TEXT_QUOTE_LINES = 3  # короткие цитаты (ответ по пунктам) не сворачиваем


def _quote_open() -> str:
    return QUOTE_OPEN.format(html.escape(tr("Показать или скрыть цитату"), quote=True))


def _text_line(line: str) -> str:
    line = URL_RE.sub(r'<a href="\1">\1</a>', html.escape(line))
    return f'<span class="q">{line}</span>' if line.lstrip().startswith("&gt;") else line


def text_fragment(text: str, fold_quotes: bool = True) -> str:
    """Обычный текст письма для страницы переписки; цитаты («> …», «-----Original Message-----»,
    «От: … Отправлено: …» и всё после) сворачиваются."""
    lines = (text or "").splitlines()
    rest_at = None
    if fold_quotes:
        for i, line in enumerate(lines):
            if TEXT_QUOTE_HEADER_RE.match(line) or (
                    TEXT_FROM_RE.match(line) and any(TEXT_SENT_RE.match(n) for n in lines[i + 1:i + 5])):
                rest_at = i
                break
    head = lines if rest_at is None else lines[:rest_at]
    out = ""

    def add(chunk: str, block: bool = False):
        nonlocal out
        if out and not block and not out.endswith("</details>"):
            out += "\n"
        out += chunk

    i = 0
    while i < len(head):
        j = i
        while fold_quotes and j < len(head) and head[j].lstrip().startswith(">"):
            j += 1
        if j - i >= MIN_TEXT_QUOTE_LINES:
            add(_quote_open() + "\n".join(_text_line(n) for n in head[i:j]) + "</details>", block=True)
            i = j
        else:
            add(_text_line(head[i]))
            i += 1
    if rest_at is not None:
        add(_quote_open() + "\n".join(_text_line(n) for n in lines[rest_at:]) + "</details>", block=True)
    return out


def _scope_selector(selector: str, scope: str) -> str:
    selector = re.sub(r"^(?::root|html)(?![\w-])\s*", "", selector.strip(), flags=re.IGNORECASE)
    match = re.match(r"body(?![\w-])", selector, re.IGNORECASE)
    if match:
        return scope + selector[match.end():]
    return f"{scope} {selector}" if selector else scope


def scope_css(css: str, scope: str) -> str:
    """Стили письма действуют только внутри него (scope — селектор блока письма),
    иначе одно письмо переписки меняло бы вид остальных и самой страницы."""
    css = re.sub(r"/\*.*?\*/|<!--|-->", "", css or "", flags=re.DOTALL)
    out, i = [], 0
    while True:
        start = css.find("{", i)
        if start < 0:
            break
        prelude = css[i:start].rsplit(";", 1)[-1].rsplit("}", 1)[-1].strip()  # @import …; @charset …;
        depth, end = 0, start
        while end < len(css):
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            if depth == 0:
                break
            end += 1
        block, i = css[start + 1:end], end + 1
        if prelude.lower().startswith(("@media", "@supports")):
            out.append(prelude + "{" + scope_css(block, scope) + "}")
        elif prelude and not prelude.startswith("@"):  # @font-face, @page, @keyframes не нужны
            selectors = [_scope_selector(s, scope) for s in prelude.split(",") if s.strip()]
            out.append(",".join(selectors) + "{" + block + "}")
    return "\n".join(out)


def thread_style() -> str:
    """Оформление страницы переписки по цветам темы (вызывать в потоке интерфейса).
    Каждое письмо — отдельная карточка с рамкой и затенённой шапкой на фоне окна."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    palette = app.palette() if app else QPalette()
    muted = palette.color(QPalette.PlaceholderText).name()
    line = palette.color(QPalette.Mid).name()
    accent = palette.color(QPalette.Highlight).name()
    page = palette.color(QPalette.Window).name()
    card = palette.color(QPalette.Base).name()
    head = palette.color(QPalette.AlternateBase).name()
    return (message_style() +
            f"body{{margin:0;padding-bottom:10px;font-family:'Segoe UI',sans-serif;font-size:10pt;background-color:{page};}}"
            f".tbar{{padding:5px 12px;color:{muted};font-size:9pt;}}"
            ".tbar a{text-decoration:none;}"
            f"details.msg{{margin:0 10px 10px 10px;border:1px solid {line};border-radius:6px;background-color:{card};}}"
            f"details.msg>summary{{display:block;list-style:none;cursor:pointer;padding:6px 10px 5px 10px;"
            f"background-color:{head};border-radius:5px;}}"
            f"details.msg[open]>summary{{border-bottom:1px solid {line};border-radius:5px 5px 0 0;}}"
            "details.msg>summary::-webkit-details-marker{display:none;}"
            f"details.msg.sel{{border-color:{accent};box-shadow:0 0 0 1px {accent};}}"
            f"details.msg.sel>summary{{border-left:4px solid {accent};padding-left:6px;}}"
            "details.msg>summary b{font-size:10.5pt;}"
            f".addr,.when,.to,.snip,.tag{{color:{muted};font-weight:normal;}}"
            ".when{float:right;margin-left:8px;}"
            f".tag{{border:1px solid {line};border-radius:3px;padding:0 4px;font-size:8pt;margin-left:6px;}}"
            ".to,.snip{font-size:9pt;}"
            ".snip{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
            "details[open]>summary .snip,details:not([open])>summary .to{display:none;}"
            ".mb{padding:8px 12px 12px 12px;overflow-x:auto;}"
            ".mb.text{white-space:pre-wrap;}"
            f"details.quote>summary{{display:inline-block;list-style:none;cursor:pointer;color:{muted};"
            f"border:1px solid {line};border-radius:3px;padding:0 6px;font-size:8pt;line-height:12px;margin:4px 0;"
            "white-space:normal;}"
            "details.quote>summary::-webkit-details-marker{display:none;}")


@dataclass
class ThreadPage:
    html: str
    inline: dict = field(default_factory=dict)
    blocked_ids: list = field(default_factory=list)   # письма со скрытыми картинками из интернета
    blocked_senders: set = field(default_factory=set)
    allow_remote: bool = False                        # разрешить загрузку из интернета странице


def _snippet(text: str) -> str:
    lines = [n for n in (text or "").splitlines() if not n.lstrip().startswith(">")]
    return " ".join(" ".join(lines).split())[:240]


def _block_header(m, own_folder_tag: bool) -> str:
    from email.utils import getaddresses, parseaddr
    from .header import human_date
    name, addr = parseaddr(m.sender or "")
    if not addr and "@" not in (m.sender or ""):
        name, addr = (m.sender or "").strip(), ""
    if addr and name and name.lower() != addr.lower():
        who = f"<b>{html.escape(name)}</b> <span class=\"addr\">&lt;{html.escape(addr)}&gt;</span>"
    else:
        who = f"<b>{html.escape(addr or name or tr('(без отправителя)'))}</b>"
    tag = f"<span class=\"tag\">{html.escape(tr('Отправленные'))}</span>" if own_folder_tag else ""
    rcpt = ", ".join(n or a for n, a in getaddresses([m.recipients or ""]) if n or a)
    to = f"<span class=\"to\">{html.escape(tr('Кому: {0}', rcpt))}</span>" if rcpt else ""
    snippet = _snippet(m.body_text) if getattr(m, "body_downloaded", True) else tr("Содержимое ещё не загружено")
    return (f"<span class=\"when\">{html.escape(human_date(m.date_ts, full=True))}</span>{who}{tag}"
            f"<br>{to}<span class=\"snip\">{html.escape(snippet)}</span>")


# Где читает пользователь: прокрутка страницы и свёрнутые письма переписки. Письмо, которое сейчас
# вверху окна, и насколько оно прокручено — чтобы новое письмо в переписке не сдвигало чтение.
_CAPTURE_VIEW_JS = (
    "(function(){var y=window.scrollY,top='',off=y,open={};"
    "document.querySelectorAll('body>details.msg').forEach(function(e){open[e.id]=e.open;"
    "var r=e.getBoundingClientRect();if(!top&&r.bottom>0){top=e.id;off=-r.top;}});"
    "return JSON.stringify({y:y,top:top,off:off,open:open});})()")
_RESTORE_VIEW_JS = (
    "(function(s){var open=s.open||{};Object.keys(open).forEach(function(id){"
    "var e=document.getElementById(id);if(e&&e.tagName==='DETAILS')e.open=open[id];});"
    "var e=s.top?document.getElementById(s.top):null;"
    "window.scrollTo(0,e?e.getBoundingClientRect().top+window.scrollY+s.off:s.y);})(%s)")

# Same-thread body arrivals do not navigate WebEngine. Keep unchanged cards as
# the same DOM nodes, including any text selection in the message being read.
_PATCH_THREAD_JS = """(function(page,previous,path,serial){
if(window.location.pathname!==path || (window.simplemailPatchSerial||0)>serial)return false;
var incoming=new DOMParser().parseFromString(page,'text/html');
var canonical=new DOMParser().parseFromString(window.simplemailCanonicalThreadPage||previous,'text/html');
var oldCards=Array.from(document.querySelectorAll('body>details.msg'));
var newCards=Array.from(incoming.querySelectorAll('body>details.msg'));
if(oldCards.length!==newCards.length || oldCards.some(function(e,i){return e.id!==newCards[i].id;}))return false;
window.simplemailPatchSerial=serial;
var y=window.scrollY,top='',off=y;
oldCards.forEach(function(e){var r=e.getBoundingClientRect();if(!top&&r.bottom>0){top=e.id;off=-r.top;}});
var styles=Array.from(document.head.querySelectorAll('style'));
var nextStyles=Array.from(incoming.head.querySelectorAll('style'));
if(styles.map(function(e){return e.outerHTML;}).join('')!==nextStyles.map(function(e){return e.outerHTML;}).join('')){
styles.forEach(function(e){e.remove();});nextStyles.forEach(function(e){document.head.appendChild(e);});}
oldCards.forEach(function(old,i){var next=newCards[i],before=canonical.getElementById(old.id);
if(before){var left=before.cloneNode(true),right=next.cloneNode(true);
[left,right].forEach(function(e){e.removeAttribute('open');e.classList.remove('sel');});
if(left.isEqualNode(right))return;}
next.open=old.open;
next.classList.toggle('sel',old.classList.contains('sel'));
old.replaceWith(next);});
var bar=document.querySelector('body>.tbar'),nextBar=incoming.querySelector('body>.tbar');
if(bar&&nextBar&&!bar.isEqualNode(nextBar))bar.replaceWith(nextBar);
var anchor=top?document.getElementById(top):null;
window.scrollTo(0,anchor?anchor.getBoundingClientRect().top+window.scrollY+off:y);
window.simplemailCanonicalThreadPage=page;
return true;})(%s,%s,%s,%s)"""


def build_thread_page(items, selected_id: int, allowed, collapsed: bool, style: str,
                      newest_first: bool = False) -> ThreadPage:
    """HTML-страница переписки. items — письма по времени (старые первыми); allowed — id писем,
    для которых разрешены картинки из интернета; collapsed — свернуть все, кроме выбранного письма;
    newest_first — новые письма сверху, старые внизу.
    Без обращения к виджетам: можно строить в фоновом потоке."""
    parsed = {}
    for m in items:
        if getattr(m, "body_downloaded", True) and m.body_html:
            p = _MailHTML(fragment=True, fold_quotes=True)
            p.feed(m.body_html)
            parsed[m.id] = (p, p.finish())
    remote_ids = [m.id for m in items if m.id in parsed and parsed[m.id][0].remote]
    allow_remote = any(i in allowed for i in remote_ids)
    page = ThreadPage(html="", allow_remote=allow_remote)
    blocks, styles = [], []
    for m in (list(reversed(items)) if newest_first else items):
        if not getattr(m, "body_downloaded", True):
            waiting = tr("Содержимое ещё загружается. Нажмите на письмо, чтобы загрузить его первым.")
            body_html = f'<div class="mb text">{html.escape(waiting)}</div>'
        elif m.id in parsed:
            p, body = parsed[m.id]
            if m.id in remote_ids and m.id not in allowed:
                page.blocked_ids.append(m.id)
                page.blocked_senders.add(m.sender or "")
                if allow_remote:  # другим письмам картинки разрешены: у этого вырезаем внешние
                    p = _MailHTML(local_images_only=True, fragment=True, fold_quotes=True)
                    p.feed(m.body_html)
                    body = p.finish()
            if m.raw and "cid:" in m.body_html.lower():
                page.inline.update(inline_parts(m.raw))
            styles.append(scope_css("\n".join(p.styles), f"#m{m.id}>.mb"))
            body_html = f"<div class=\"mb\">{body}</div>"
        else:
            body_html = f"<div class=\"mb text\">{text_fragment(m.body_text)}</div>"
        selected = m.id == selected_id
        attrs = " class=\"msg sel\"" if selected else " class=\"msg\""
        opened = " open" if selected or not collapsed else ""
        blocks.append(f"<a name=\"m{m.id}\"></a><details id=\"m{m.id}\"{attrs}{opened}><summary>"
                      f"{_block_header(m, m.folder == 'sent')}</summary>{body_html}</details>")
    bar = (f"<div class=\"tbar\">{html.escape(tr('Писем в переписке: {0}', len(items)))} &nbsp;·&nbsp; "
           f"<a href=\"simplemail:/cmd/expand\">{html.escape(tr('Развернуть все'))}</a> &nbsp;·&nbsp; "
           f"<a href=\"simplemail:/cmd/collapse\">{html.escape(tr('Свернуть все, кроме этого'))}</a> &nbsp;·&nbsp; "
           + (f"<a href=\"simplemail:/cmd/oldest\">{html.escape(tr('Старые сверху'))}</a>" if newest_first else
              f"<a href=\"simplemail:/cmd/newest\">{html.escape(tr('Новые сверху'))}</a>") + "</div>")
    page.html = ("<html><head><meta charset='utf-8'><style>" + style + "</style><style>" + "\n".join(styles)
                 + "</style></head><body>" + bar + "".join(blocks) + "</body></html>")
    return page


if HAS_WEBENGINE:
    class _SchemeHandler(QWebEngineUrlSchemeHandler):
        def __init__(self, view: "MessageView"):
            super().__init__(view)
            self.view = view

        def requestStarted(self, job):
            url = job.requestUrl()
            if url.scheme() == "cid":
                part = self.view._inline.get(url.path())
                if not part:
                    job.fail(job.Error.UrlNotFound)
                    return
                mime, data = part
            else:
                mime, data = "text/html", self.view._page_html.encode("utf-8")
            buf = QBuffer(job)
            buf.setData(QByteArray(data))
            job.reply(mime.encode(), buf)

    class _Interceptor(QWebEngineUrlRequestInterceptor):
        """Блокирует загрузку из интернета, пока не разрешено."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.allow_remote = False

        def interceptRequest(self, info):
            scheme = info.requestUrl().scheme()
            if scheme in ("simplemail", "cid", "data", "about"):
                return
            if scheme in ("http", "https") and self.allow_remote:
                return
            info.block(True)

    class _Page(QWebEnginePage):
        def __init__(self, profile, parent, view: "MessageView"):
            super().__init__(profile, parent)
            self.view = view

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if url.scheme() == "simplemail" and url.path().startswith("/cmd/"):
                if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                    QTimer.singleShot(0, lambda path=url.path(): self.view.run_command(path))
                return False
            if url.scheme() in ("simplemail", "about", "data"):
                return True
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                self.view.open_link(url)
            return False

        def createWindow(self, window_type):
            """Ссылка с target="_blank" (так сделаны почти все кнопки в рассылках) идёт не через
            acceptNavigationRequest, а просит новое окно. Без своей страницы щелчок пропадал."""
            return _NewWindowCatcher(self.profile(), self.view)

    class _NewWindowCatcher(QWebEnginePage):
        """Временная страница для «нового окна»: её первый адрес открывается как обычная ссылка
        (http/https — в браузере, mailto — окно письма), сама страница ничего не загружает."""
        def __init__(self, profile, view: "MessageView"):
            super().__init__(profile, view)
            self.view = view
            self.done = False
            QTimer.singleShot(10000, self.deleteLater)  # если адрес так и не пришёл

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if not self.done and url.scheme() != "about":
                self.done = True
                QTimer.singleShot(0, lambda: self.view.open_link(url))
                QTimer.singleShot(0, self.deleteLater)
            return False


class MessageView(QWidget):
    remote_allowed_for_sender = Signal(str)  # пользователь выбрал «всегда показывать» для отправителя
    state_changed = Signal()                 # показано другое письмо / загружены картинки
    remote_loaded = Signal(object)           # пользователь нажал «Показать картинки» (письмо)
    mailto_requested = Signal(dict)          # щелчок по mailto: — открыть окно письма
    link_blocked = Signal(str)               # ссылка с опасной схемой не открыта
    link_hovered = Signal(str)               # адрес ссылки под мышью ("" — мышь ушла со ссылки)
    thread_order_changed = Signal(bool)      # на странице переписки выбран порядок: True — новые сверху
    thread_message_clicked = Signal(int)     # щелчок по другому письму на странице переписки (id письма)

    def __init__(self, parent=None, defer_web: bool = False):
        """defer_web — встроенный браузер создаётся после первой отрисовки окна: запуск Chromium
        занимает около секунды, и главное окно без этого появляется заметно быстрее."""
        super().__init__(parent)
        self._page_html = ""
        self._inline: dict[str, tuple[str, bytes]] = {}
        self._msg = None
        self._thread = None  # показана переписка: {items, allowed, collapsed, newest_first, page}
        self._counter = 0
        self._allow_remote = False
        self._loading = False       # страница ещё загружается
        self._press_pos = None      # где нажата левая кнопка мыши на странице
        self._click_target = None   # виджет WebEngine, которому поставлен фильтр щелчков
        self._dark = is_dark()
        self._uses_webengine = HAS_WEBENGINE
        self._shutdown = False
        self._restore_view = None   # прокрутка, которую вернуть после загрузки страницы
        self._keeping_view = False  # страница перестраивается с прежней прокруткой: без перехода к якорю
        self._pending_thread_refresh = None
        self._thread_patch_serial = 0
        self.web = None
        self._web_pending = False   # браузер ещё будет создан (defer_web)
        self._deferred_render = None  # страница, показанная до создания браузера

        # полоса «картинки скрыты»
        self.banner = QFrame()
        self.banner.setProperty("tone", "warning")
        bl = QHBoxLayout(self.banner); bl.setContentsMargins(8, 4, 8, 4)
        bl.addWidget(QLabel(tr("Картинки из интернета скрыты для защиты от слежки.")), 1)
        self.show_btn = QPushButton(tr("Показать картинки"))
        self.show_btn.clicked.connect(self.load_remote)
        self.trust_btn = QPushButton(tr("Всегда показывать от этого отправителя"))
        self.trust_btn.clicked.connect(self._trust_sender)
        bl.addWidget(self.show_btn); bl.addWidget(self.trust_btn)
        self.banner.hide()

        holder = QWidget()
        self.stack = QStackedLayout(holder)
        if self._uses_webengine:
            if defer_web:
                self._web_pending = True
                self._placeholder = _FirstPaint(self._web_placeholder_painted)
                self.stack.addWidget(self._placeholder)
            else:
                self._create_web()
        else:
            self.web = None
            self.text = _SafeTextBrowser(self)
            self.show_btn.setEnabled(False)
            self.trust_btn.setEnabled(False)
            self.show_btn.setToolTip(tr("Для загрузки внешних картинок требуется Qt WebEngine"))
            self.text.setOpenLinks(False)
            self.text.anchorClicked.connect(self.open_link)
            self.text.highlighted.connect(self._link_hovered)
            self.stack.addWidget(self.text)

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.banner)
        lay.addWidget(holder, 1)
        self._apply_theme()


    # ---------- встроенный браузер ----------
    def _web_placeholder_painted(self):
        # Окно уже нарисовано — теперь можно запускать Chromium.
        QTimer.singleShot(0, self._create_web)

    def showEvent(self, event):
        super().showEvent(event)
        if self._web_pending:
            # Если область письма не видна (свёрнута разделителем), отрисовки может не быть.
            QTimer.singleShot(1500, self._create_web)

    def _create_web(self):
        if self.web is not None or self._shutdown or not self._uses_webengine:
            return
        placeholder = getattr(self, "_placeholder", None)
        started = time.perf_counter()
        self.profile = QWebEngineProfile(self)  # отдельный профиль без сохранения на диск
        self.interceptor = _Interceptor(self)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.handler = _SchemeHandler(self)
        self.profile.installUrlSchemeHandler(SCHEME, self.handler)
        self.profile.installUrlSchemeHandler(b"cid", self.handler)
        self.web = QWebEngineView()
        page = _Page(self.profile, self.web, self)
        s = page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.web.setPage(page)
        self.web.loadFinished.connect(self._load_finished)
        page.linkHovered.connect(self._link_hovered)
        # своё меню по правой кнопке: в стандартном «Назад», «Обновить», «Сохранить страницу»,
        # «Открыть в новой вкладке» и т.п. в окне письма не работают или вредны
        self.web.setContextMenuPolicy(Qt.CustomContextMenu)
        self.web.customContextMenuRequested.connect(self._context_menu)
        self.stack.addWidget(self.web)
        self._web_pending = False
        if placeholder is not None:
            self.stack.removeWidget(placeholder)
            placeholder.deleteLater()
            self._placeholder = None
        self.stack.setCurrentWidget(self.web)
        self._apply_web_theme()
        if placeholder is not None:
            logging.getLogger("simplemail").info(
                "Запуск: просмотр писем (WebEngine) готов за %.1f с", time.perf_counter() - started)
        deferred, self._deferred_render = self._deferred_render, None
        if deferred is not None:
            self._render(*deferred)

    # ---------- API ----------
    def open_link(self, url: QUrl):
        """Ссылки из письма: http(s) — во внешнем браузере, mailto — своё окно, остальное блокируем."""
        if url.scheme() == "simplemail" and url.path().startswith("/cmd/"):
            self.run_command(url.path())
            return
        action = link_action(url)
        if action == "open":
            QDesktopServices.openUrl(url)
        elif action == "mailto":
            self.mailto_requested.emit(parse_mailto(url))
        else:
            self.link_blocked.emit(url.toDisplayString()[:200])

    def _context_menu(self, pos):
        if self.web is None:
            return
        request = self.web.lastContextMenuRequest()
        page = self.web.page()
        menu = QMenu(self)
        url = request.linkUrl() if request is not None else QUrl()
        if url.isValid() and not url.isEmpty() and url.scheme() not in ("simplemail", "cid", "about", "data"):
            mailto = url.scheme().lower() == "mailto"
            menu.addAction(tr("Написать письмо") if mailto else tr("Открыть ссылку в браузере"),
                           lambda: self.open_link(url))
            text = parse_mailto(url).get("to", "") if mailto else url.toString()
            menu.addAction(tr("Копировать адрес") if mailto else tr("Копировать адрес ссылки"),
                           lambda: QApplication.clipboard().setText(text))
            menu.addSeparator()
        copy = menu.addAction(tr("Копировать"), lambda: page.triggerAction(QWebEnginePage.WebAction.Copy))
        copy.setEnabled(bool(page.selectedText()))
        menu.addAction(tr("Выделить всё"), lambda: page.triggerAction(QWebEnginePage.WebAction.SelectAll))
        menu.exec(self.web.mapToGlobal(pos))

    def _link_hovered(self, url):
        """Показать, куда ведёт ссылка: текст кнопки в письме может не совпадать с адресом."""
        url = QUrl(url) if isinstance(url, str) else url
        if url.isEmpty() or url.scheme() in ("simplemail", "cid", "about", "data"):  # служебные ссылки страницы
            self.link_hovered.emit("")
        else:
            self.link_hovered.emit(url.toDisplayString())

    def clear(self):
        empty_page = wrap_html("<html><body></body></html>")
        if self._msg is None and self._thread is None and self._page_html == empty_page:
            return
        self._msg = None
        self._thread = None
        self.banner.hide()
        self._render(empty_page, remote=False)
        self.state_changed.emit()

    def shutdown(self):
        """Удалить страницу раньше профиля (иначе WebEngine ругается при выходе)."""
        self._shutdown = True
        self._web_pending = False
        self._deferred_render = None
        if self.web is not None:
            import shiboken6
            shiboken6.delete(self.web)
            self.web = None

    def show_message(self, m, allow_remote: bool = False, keep_view: bool = False, prepared=None):
        """keep_view — перестроить страницу, оставив прокрутку как была (обновление после проверки почты)."""
        if keep_view:
            self._keep_view(lambda: self.show_message(m, allow_remote))
            return
        allow_remote = allow_remote and self._web_available()
        self._msg = m
        self._thread = None
        self.trust_btn.show()
        ready = bool(getattr(m, "body_downloaded", True))
        self._inline = (prepared[1] if prepared is not None else
                        inline_parts(m.raw) if ready and m.body_html and "cid:" in m.body_html.lower() else {})
        if not ready:
            self.banner.hide()
            self._render(text_to_html(tr("Содержимое письма ещё загружается…")), remote=False)
        elif prepared is not None:
            page, _, remote = prepared
            self.banner.setVisible(remote and not allow_remote)
            self._render(page, remote=allow_remote)
        elif m.body_html:
            remote = has_remote_content(m.body_html)
            self.banner.setVisible(remote and not allow_remote)
            self._render(wrap_html(m.body_html), remote=allow_remote)
        else:
            self.banner.hide()
            self._render(text_to_html(m.body_text), remote=False)
        self.state_changed.emit()

    def load_remote(self):
        if self._msg is None or not self._web_available():
            return
        if self._thread is not None:
            t = self._thread
            blocked = [m for m in t["items"] if m.id in t["page"].blocked_ids]
            self._rerender_thread(allowed=t["allowed"] | {m.id for m in blocked})
            for m in blocked:
                self.remote_loaded.emit(m)
            return
        self.show_message(self._msg, allow_remote=True)
        self.remote_loaded.emit(self._msg)

    # ---------- переписка ----------
    @property
    def thread_shown(self) -> bool:
        return self._thread is not None

    def show_thread(self, m, items, allowed, page: ThreadPage | None = None, collapsed: bool = False,
                    newest_first: bool = False, keep_view: bool = False):
        """Показать всю переписку: письма по времени (newest_first — новые сверху), выбранное
        письмо m выделено и прокручено в начало окна. page — уже построенная в фоне страница.
        keep_view — вместо прокрутки к письму оставить прокрутку и свёрнутые письма как были."""
        if (keep_view and self._uses_webengine and self.web is not None and self._thread is not None
                and self._msg is not None and self._msg.id == m.id):
            if self._loading:
                # The latest refresh replaces older queued refreshes. Never
                # interrupt an in-flight navigation just to update another body.
                self._pending_thread_refresh = (self._counter, m, items, set(allowed), page,
                                                collapsed, newest_first)
                return
            if ([item.id for item in items] == self.thread_ids()
                    and newest_first == self._thread["newest_first"]):
                if page is None:
                    page = build_thread_page(items, m.id, set(allowed), collapsed, thread_style(), newest_first)
                self._patch_thread(m, items, set(allowed), page, collapsed, newest_first)
                return
        if keep_view:
            self._keep_view(lambda: self.show_thread(m, items, allowed, page, collapsed, newest_first))
            return
        allowed = set(allowed)
        if page is None:
            page = build_thread_page(items, m.id, allowed, collapsed, thread_style(), newest_first)
        self._msg = m
        self._thread = {"items": items, "allowed": allowed, "collapsed": collapsed,
                        "newest_first": newest_first, "page": page}
        self._inline = page.inline
        self.banner.setVisible(bool(page.blocked_ids))
        self.trust_btn.setVisible(len(page.blocked_senders) == 1)
        self._render(page.html, remote=page.allow_remote and self._web_available(), anchor=f"m{m.id}")
        self.state_changed.emit()

    def _patch_thread(self, m, items, allowed, page, collapsed, newest_first):
        """Update changed cards in one script, retaining the current document."""
        self._thread_patch_serial += 1
        serial, counter = self._thread_patch_serial, self._counter
        previous_html = self._page_html
        self._msg = m
        self._thread = {"items": items, "allowed": allowed, "collapsed": collapsed,
                        "newest_first": newest_first, "page": page}
        # Install resource policy and CID data before DOM insertion can request
        # images; blocked remote sources were removed by build_thread_page.
        self._inline = page.inline
        self._page_html = page.html
        self._allow_remote = page.allow_remote
        self.interceptor.allow_remote = page.allow_remote
        self.banner.setVisible(bool(page.blocked_ids))
        self.trust_btn.setVisible(len(page.blocked_senders) == 1)

        def patched(ok):
            if (self._shutdown or self._counter != counter or self._thread_patch_serial != serial
                    or self._msg is None or self._msg.id != m.id):
                return
            if not ok:
                # A document changed before the script could run. The existing
                # navigation path remains the fallback, preserving scroll.
                self._keep_view(lambda: self.show_thread(m, items, allowed, page, collapsed, newest_first))

        script = _PATCH_THREAD_JS % (json.dumps(page.html), json.dumps(previous_html),
                                    json.dumps(f"/message/{counter}"), serial)
        self.web.page().runJavaScript(script, QWebEngineScript.ScriptWorldId.ApplicationWorld, patched)
        self.state_changed.emit()

    def thread_ids(self) -> list[int]:
        """id писем показанной переписки (пусто, если показано одно письмо)."""
        return [x.id for x in self._thread["items"]] if self._thread is not None else []

    def thread_state(self) -> set[tuple[int, bool]]:
        """Track body arrivals even when the visible thread keeps the same IDs."""
        return {(item.id, bool(getattr(item, "body_downloaded", True)))
                for item in self._thread["items"]} if self._thread is not None else set()

    def select_in_thread(self, m, scroll: bool = True) -> bool:
        """Сделать выбранным другое письмо показанной переписки, не перестраивая страницу:
        прокрутка и свёрнутые письма остаются как были. scroll — развернуть письмо и прокрутить к нему.
        False — письма нет в показанной переписке."""
        if self._thread is None or m.id not in self.thread_ids():
            return False
        self._msg = m
        if self.web is None or self._loading:
            self._rerender_thread()  # страница ещё не готова (или нет WebEngine) — строим заново
            return True
        # Скрипт выполняется в отдельном «мире» браузера: скрипты самих писем по-прежнему выключены.
        self.web.page().runJavaScript(
            "(function(id,scroll){"
            "document.querySelectorAll('body>details.msg.sel').forEach(function(e){e.classList.remove('sel');});"
            "var e=document.getElementById('m'+id);if(!e)return;e.classList.add('sel');"
            "if(scroll){e.open=true;e.scrollIntoView();}"
            f"}})({int(m.id)},{'true' if scroll else 'false'})",
            QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self.state_changed.emit()
        return True

    def _rerender_thread(self, **changes):
        t = dict(self._thread)
        t.update(changes)
        self.show_thread(self._msg, t["items"], t["allowed"], collapsed=t["collapsed"], newest_first=t["newest_first"])

    def set_thread_order(self, newest_first: bool):
        """Порядок писем переписки: новые сверху или старые сверху."""
        if self._thread is not None and self._msg is not None and self._thread["newest_first"] != newest_first:
            self._rerender_thread(newest_first=newest_first)

    def run_command(self, path: str):
        """Ссылки управления на странице переписки: развернуть / свернуть письма, порядок писем."""
        if self._thread is None or self._msg is None:
            return
        collapsed = {"/cmd/expand": False, "/cmd/collapse": True}.get(path)
        if collapsed is not None:
            self._rerender_thread(collapsed=collapsed)
        newest = {"/cmd/newest": True, "/cmd/oldest": False}.get(path)
        if newest is not None:
            self.set_thread_order(newest)
            self.thread_order_changed.emit(newest)

    # ---------- внутреннее ----------
    def _keep_view(self, render):
        """Перестроить страницу (render) так, чтобы читаемое место осталось на экране."""
        if self._shutdown:
            return
        if not self._uses_webengine:
            bar = self.text.verticalScrollBar()
            value = bar.value()
            render()
            self.text.document().size()  # разметка документа, чтобы у полосы прокрутки был верный диапазон
            bar.setValue(value)
            return
        if self.web is None or self._loading or not self._page_html:
            render()  # прежняя страница ещё не показана — сохранять нечего
            return
        counter, msg = self._counter, self._msg

        def captured(state):
            # Пока читали прокрутку, могли открыть другое письмо: тогда эта перерисовка не нужна.
            if self._shutdown or self._counter != counter or self._msg is not msg:
                return
            try:
                restore = json.loads(state) if isinstance(state, str) else None
            except ValueError:
                restore = None
            if not isinstance(restore, dict):
                restore = None
            self._keeping_view = restore is not None
            try:
                render()
            finally:
                self._keeping_view = False
            self._restore_view = restore

        self.web.page().runJavaScript(_CAPTURE_VIEW_JS, QWebEngineScript.ScriptWorldId.ApplicationWorld, captured)

    def _load_finished(self, ok):
        self._loading = False
        if self.web is not None:
            self.web.page().history().clear()  # «Назад» (Alt+←, кнопка мыши) в окне письма не нужен
        self._watch_clicks()
        state, self._restore_view = self._restore_view, None
        if ok and state and self.web is not None:
            self.web.page().runJavaScript(_RESTORE_VIEW_JS % json.dumps(state),
                                          QWebEngineScript.ScriptWorldId.ApplicationWorld)
        pending, self._pending_thread_refresh = self._pending_thread_refresh, None
        if pending is not None:
            counter, m, items, allowed, page, collapsed, newest_first = pending
            if (not self._shutdown and self._counter == counter and self._thread is not None
                    and self._msg is not None and self._msg.id == m.id):
                self.show_thread(m, items, allowed, page, collapsed, newest_first, keep_view=ok)

    def _watch_clicks(self):
        """Щелчки мыши по странице приходят виджету WebEngine (focusProxy); он может смениться."""
        proxy = self.web.focusProxy() if self.web is not None else None
        if proxy is not None and proxy is not self._click_target:
            proxy.installEventFilter(self)
            self._click_target = proxy

    def eventFilter(self, obj, event):
        if self._thread is not None and obj is self._click_target:
            kind = event.type()
            if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._press_pos = event.position()
            elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and self._press_pos is not None:
                moved = (event.position() - self._press_pos).manhattanLength()
                self._press_pos = None
                if moved <= QApplication.startDragDistance():  # выделение текста мышью — не щелчок
                    self._message_at(event.position())
        return super().eventFilter(obj, event)

    def _message_at(self, pos):
        """Какое письмо переписки под точкой щелчка (без JavaScript на странице, в отдельном «мире»)."""
        if self.web is None:
            return
        zoom = self.web.zoomFactor() or 1.0
        self.web.page().runJavaScript(
            f"(function(){{var e=document.elementFromPoint({pos.x() / zoom:.1f},{pos.y() / zoom:.1f});"
            "e=e&&e.closest('body>details.msg');return e?e.id:'';})()",
            QWebEngineScript.ScriptWorldId.ApplicationWorld, self._clicked_block)

    def _clicked_block(self, block_id):
        if self._thread is None or not isinstance(block_id, str) or not block_id.startswith("m"):
            return
        try:
            mid = int(block_id[1:])
        except ValueError:
            return
        if mid in self.thread_ids() and (self._msg is None or mid != self._msg.id
                                        or not getattr(self._msg, "body_downloaded", True)):
            self.thread_message_clicked.emit(mid)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange and hasattr(self, "web"):
            QTimer.singleShot(0, self._apply_theme)

    def _apply_theme(self):
        if self._shutdown:
            return
        changed = self._dark != is_dark()
        self._dark = is_dark()
        self._apply_web_theme()
        if changed or not self._page_html:
            if self._msg is None:
                self.clear()
            elif self._thread is not None:
                self._rerender_thread()
            else:
                self.show_message(self._msg, allow_remote=self._allow_remote)

    def _apply_web_theme(self):
        if self.web is not None:
            page = self.web.page()
            page.setBackgroundColor(self.palette().color(QPalette.Base))
            force_dark = getattr(QWebEngineSettings.WebAttribute, "ForceDarkMode", None)
            if force_dark is not None:
                page.settings().setAttribute(force_dark, self._dark)

    def _web_available(self) -> bool:
        """Встроенный браузер есть или вот-вот будет (создаётся после отрисовки окна)."""
        return self.web is not None or self._web_pending

    def _trust_sender(self):
        if self._msg is None:
            return
        if self._thread is not None:
            senders = self._thread["page"].blocked_senders
            if len(senders) == 1:
                self.remote_allowed_for_sender.emit(next(iter(senders)))
        else:
            self.remote_allowed_for_sender.emit(self._msg.sender)
        self.load_remote()

    def _render(self, page_html: str, remote: bool, anchor: str = ""):
        if self._shutdown:
            return
        self._page_html = page_html
        self._pending_thread_refresh = None
        self._allow_remote = remote
        self._restore_view = None  # прокрутка прежней страницы к новой не относится
        self.link_hovered.emit("")  # мышь могла остаться над ссылкой прежней страницы
        if self._uses_webengine:
            if self.web is None:
                if self._web_pending:  # покажем, как только браузер будет создан
                    self._deferred_render = (page_html, remote, anchor)
                return
            self.interceptor.allow_remote = remote
            self._loading = True
            self._counter += 1  # новый адрес, чтобы браузер не брал страницу из кэша
            url = QUrl(f"simplemail:/message/{self._counter}")
            if anchor and not self._keeping_view:
                url.setFragment(anchor)  # прокрутка к выбранному письму (работает без JavaScript)
            self.web.setUrl(url)
            self._watch_clicks()
        else:
            self.text.setHtml(page_html)
            if anchor:
                self.text.scrollToAnchor(anchor)
            if self._dark and self._msg is not None and (self._msg.body_html or self._thread is not None):
                # QTextBrowser не поддерживает ForceDarkMode: убираем только
                # цвета текста/подложки в отображаемой копии, сохраняя формат.
                cursor = QTextCursor(self.text.document())
                cursor.select(QTextCursor.Document)
                fmt = QTextCharFormat()
                fmt.setForeground(self.palette().color(QPalette.Text))
                fmt.setBackground(self.palette().color(QPalette.Base))
                cursor.mergeCharFormat(fmt)
