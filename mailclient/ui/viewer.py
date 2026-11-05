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
import os
import re
import base64
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote_to_bytes
from email import policy

from PySide6.QtCore import QBuffer, QByteArray, QEvent, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout, QTextBrowser, QVBoxLayout, QWidget,
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


def _page_style() -> str:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    palette = app.palette() if app else QPalette()
    return (f"body{{color:{palette.color(QPalette.Text).name()};"
            f"background-color:{palette.color(QPalette.Base).name()};}}"
            f"a{{color:{palette.color(QPalette.Link).name()};}}"
            f".q{{color:{palette.color(QPalette.PlaceholderText).name()};}}")


def text_to_html(text: str) -> str:
    lines = []
    for line in html.escape(text or "").splitlines():
        line = URL_RE.sub(r'<a href="\1">\1</a>', line)
        if line.startswith("&gt;"):
            line = f'<span class="q">{line}</span>'
        lines.append(line)
    return ("<html><head><meta charset='utf-8'><style>"
            "body{font-family:'Segoe UI',sans-serif;font-size:10pt;white-space:pre-wrap;margin:8px;}"
            + _page_style() + "</style></head><body>" + "\n".join(lines) + "</body></html>")


def sanitize_html(body: str, *, local_images_only=False) -> str:
    parser = _MailHTML(local_images_only=local_images_only)
    parser.feed(body)
    parser.close()
    return "".join(parser.output)


def wrap_html(body: str) -> str:
    return "<meta charset='utf-8'><style>" + _page_style() + "</style>" + sanitize_html(body)


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
    return (_page_style() +
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
    return (f"<span class=\"when\">{html.escape(human_date(m.date_ts, full=True))}</span>{who}{tag}"
            f"<br>{to}<span class=\"snip\">{html.escape(_snippet(m.body_text))}</span>")


def build_thread_page(items, selected_id: int, allowed, collapsed: bool, style: str,
                      newest_first: bool = False) -> ThreadPage:
    """HTML-страница переписки. items — письма по времени (старые первыми); allowed — id писем,
    для которых разрешены картинки из интернета; collapsed — свернуть все, кроме выбранного письма;
    newest_first — новые письма сверху, старые внизу.
    Без обращения к виджетам: можно строить в фоновом потоке."""
    parsed = {}
    for m in items:
        if m.body_html:
            p = _MailHTML(fragment=True, fold_quotes=True)
            p.feed(m.body_html)
            parsed[m.id] = (p, p.finish())
    remote_ids = [m.id for m in items if m.id in parsed and parsed[m.id][0].remote]
    allow_remote = any(i in allowed for i in remote_ids)
    page = ThreadPage(html="", allow_remote=allow_remote)
    blocks, styles = [], []
    for m in (list(reversed(items)) if newest_first else items):
        if m.id in parsed:
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


class MessageView(QWidget):
    remote_allowed_for_sender = Signal(str)  # пользователь выбрал «всегда показывать» для отправителя
    state_changed = Signal()                 # показано другое письмо / загружены картинки
    remote_loaded = Signal(object)           # пользователь нажал «Показать картинки» (письмо)
    mailto_requested = Signal(dict)          # щелчок по mailto: — открыть окно письма
    link_blocked = Signal(str)               # ссылка с опасной схемой не открыта
    thread_order_changed = Signal(bool)      # на странице переписки выбран порядок: True — новые сверху
    thread_message_clicked = Signal(int)     # щелчок по другому письму на странице переписки (id письма)

    def __init__(self, parent=None):
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
            self.stack.addWidget(self.web)
        else:
            self.web = None
            self.text = _SafeTextBrowser(self)
            self.show_btn.setEnabled(False)
            self.trust_btn.setEnabled(False)
            self.show_btn.setToolTip(tr("Для загрузки внешних картинок требуется Qt WebEngine"))
            self.text.setOpenLinks(False)
            self.text.anchorClicked.connect(self.open_link)
            self.stack.addWidget(self.text)

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.banner)
        lay.addWidget(holder, 1)
        self._apply_theme()

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

    def clear(self):
        self._msg = None
        self._thread = None
        self.banner.hide()
        self._render(wrap_html("<html><body></body></html>"), remote=False)
        self.state_changed.emit()

    def shutdown(self):
        """Удалить страницу раньше профиля (иначе WebEngine ругается при выходе)."""
        self._shutdown = True
        if self.web is not None:
            import shiboken6
            shiboken6.delete(self.web)
            self.web = None

    def show_message(self, m, allow_remote: bool = False):
        allow_remote = allow_remote and self.web is not None
        self._msg = m
        self._thread = None
        self.trust_btn.show()
        self._inline = inline_parts(m.raw) if m.body_html else {}
        if m.body_html:
            remote = has_remote_content(m.body_html)
            self.banner.setVisible(remote and not allow_remote)
            self._render(wrap_html(m.body_html), remote=allow_remote)
        else:
            self.banner.hide()
            self._render(text_to_html(m.body_text), remote=False)
        self.state_changed.emit()

    def load_remote(self):
        if self._msg is None or self.web is None:
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
                    newest_first: bool = False):
        """Показать всю переписку: письма по времени (newest_first — новые сверху), выбранное
        письмо m выделено и прокручено в начало окна. page — уже построенная в фоне страница."""
        allowed = set(allowed)
        if page is None:
            page = build_thread_page(items, m.id, allowed, collapsed, thread_style(), newest_first)
        self._msg = m
        self._thread = {"items": items, "allowed": allowed, "collapsed": collapsed,
                        "newest_first": newest_first, "page": page}
        self._inline = page.inline
        self.banner.setVisible(bool(page.blocked_ids))
        self.trust_btn.setVisible(len(page.blocked_senders) == 1)
        self._render(page.html, remote=page.allow_remote and self.web is not None, anchor=f"m{m.id}")
        self.state_changed.emit()

    def thread_ids(self) -> list[int]:
        """id писем показанной переписки (пусто, если показано одно письмо)."""
        return [x.id for x in self._thread["items"]] if self._thread is not None else []

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
    def _load_finished(self, _ok):
        self._loading = False
        self._watch_clicks()

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
        if mid in self.thread_ids() and (self._msg is None or mid != self._msg.id):
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
        if self.web is not None:
            page = self.web.page()
            page.setBackgroundColor(self.palette().color(QPalette.Base))
            force_dark = getattr(QWebEngineSettings.WebAttribute, "ForceDarkMode", None)
            if force_dark is not None:
                page.settings().setAttribute(force_dark, self._dark)
        if changed or not self._page_html:
            if self._msg is None:
                self.clear()
            elif self._thread is not None:
                self._rerender_thread()
            else:
                self.show_message(self._msg, allow_remote=self._allow_remote)

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
        self._allow_remote = remote
        if self._uses_webengine:
            if self.web is None:
                return
            self.interceptor.allow_remote = remote
            self._loading = True
            self._counter += 1  # новый адрес, чтобы браузер не брал страницу из кэша
            url = QUrl(f"simplemail:/message/{self._counter}")
            if anchor:
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
