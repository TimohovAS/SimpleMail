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
from html.parser import HTMLParser
from urllib.parse import unquote_to_bytes
from email import policy

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout, QTextBrowser, QVBoxLayout, QWidget,
)

from .theme import is_dark
from .image_resources import raster_image

try:
    from PySide6.QtWebEngineCore import (
        QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor,
        QWebEngineUrlScheme, QWebEngineUrlSchemeHandler,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except Exception:  # WebEngine не установлен — работаем через простой просмотрщик
    HAS_WEBENGINE = False

SCHEME = b"simplemail"
URL_RE = re.compile(r"(https?://[^\s<>\"']+)")

CSS_REMOTE_RE = re.compile(r"(?:url\(\s*[\"']?\s*|@import\s*[\"']\s*)(?:https?:)?//", re.IGNORECASE)


class _MailHTML(HTMLParser):
    DROP_CONTENT = {"script", "object", "embed", "iframe", "frame", "frameset", "applet"}
    DROP_TAG = {"base", "meta", "link", "form", "input", "button"}

    def __init__(self, local_images_only=False):
        super().__init__(convert_charrefs=False)
        self.local_images_only = local_images_only
        self.in_style = False
        self.output, self.blocked = [], []
        self.remote = False

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
        if tag in self.DROP_CONTENT or (self.local_images_only and tag == "svg"):
            if tag not in {"embed", "frame"}:
                self.blocked.append(tag)
            return
        if not self.blocked and tag not in self.DROP_TAG:
            if self.local_images_only:
                source = (dict(attrs).get("src") or "").lower()
                if tag == "img" and not (source.startswith("cid:") or re.match(r"data:image/(?:png|jpeg|gif|bmp|webp|tiff|x-icon)[;,]", source)):
                    self.output.append(html.escape(dict(attrs).get("alt") or ""))
                    return
                attrs = [(key, self._local_style(value or "") if key == "style" else value)
                         for key, value in attrs if key not in {"background", "srcset"}]
                self.output.append("<" + tag + "".join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in attrs) + ">")
            else:
                self.output.append(self.get_starttag_text())

    @staticmethod
    def _local_style(data):
        return re.sub(r"url\([^)]*\)|@import[^;]*;?", "", data, flags=re.IGNORECASE)

    def handle_startendtag(self, tag, attrs):
        previous = len(self.blocked)
        self.handle_starttag(tag, attrs)
        del self.blocked[previous:]

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if self.blocked:
            if self.blocked[-1] == tag:
                self.blocked.pop()
        elif tag not in self.DROP_CONTENT | self.DROP_TAG:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if CSS_REMOTE_RE.search(data):
            self.remote = True
        if not self.blocked:
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
            if url.scheme() in ("simplemail", "about", "data"):
                return True
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                self.view.open_link(url)
            return False


class MessageView(QWidget):
    remote_allowed_for_sender = Signal(str)  # пользователь выбрал «всегда показывать» для отправителя
    state_changed = Signal()                 # показано другое письмо / загружены картинки
    mailto_requested = Signal(dict)          # щелчок по mailto: — открыть окно письма
    link_blocked = Signal(str)               # ссылка с опасной схемой не открыта

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page_html = ""
        self._inline: dict[str, tuple[str, bytes]] = {}
        self._msg = None
        self._counter = 0
        self._allow_remote = False
        self._dark = is_dark()

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
        if HAS_WEBENGINE:
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
        action = link_action(url)
        if action == "open":
            QDesktopServices.openUrl(url)
        elif action == "mailto":
            self.mailto_requested.emit(parse_mailto(url))
        else:
            self.link_blocked.emit(url.toDisplayString()[:200])

    def clear(self):
        self._msg = None
        self.banner.hide()
        self._render(wrap_html("<html><body></body></html>"), remote=False)
        self.state_changed.emit()

    def shutdown(self):
        """Удалить страницу раньше профиля (иначе WebEngine ругается при выходе)."""
        if self.web is not None:
            import shiboken6
            shiboken6.delete(self.web)
            self.web = None

    def show_message(self, m, allow_remote: bool = False):
        allow_remote = allow_remote and self.web is not None
        self._msg = m
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
        if self._msg is not None:
            self.show_message(self._msg, allow_remote=True)

    # ---------- внутреннее ----------
    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange and hasattr(self, "web"):
            QTimer.singleShot(0, self._apply_theme)

    def _apply_theme(self):
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
            else:
                self.show_message(self._msg, allow_remote=self._allow_remote)

    def _trust_sender(self):
        if self._msg is not None:
            self.remote_allowed_for_sender.emit(self._msg.sender)
            self.load_remote()

    def _render(self, page_html: str, remote: bool):
        self._page_html = page_html
        self._allow_remote = remote
        if HAS_WEBENGINE:
            if self.web is None:
                return
            self.interceptor.allow_remote = remote
            self._counter += 1  # новый адрес, чтобы браузер не брал страницу из кэша
            self.web.setUrl(QUrl(f"simplemail:/message/{self._counter}"))
        else:
            self.text.setHtml(page_html)
            if self._dark and self._msg is not None and self._msg.body_html:
                # QTextBrowser не поддерживает ForceDarkMode: убираем только
                # цвета текста/подложки в отображаемой копии, сохраняя формат.
                cursor = QTextCursor(self.text.document())
                cursor.select(QTextCursor.Document)
                fmt = QTextCharFormat()
                fmt.setForeground(self.palette().color(QPalette.Text))
                fmt.setBackground(self.palette().color(QPalette.Base))
                cursor.mergeCharFormat(fmt)
