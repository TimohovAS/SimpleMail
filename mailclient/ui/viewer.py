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
from email import policy

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QStackedLayout, QTextBrowser, QVBoxLayout, QWidget,
)

from .theme import is_dark

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
REMOTE_RE = re.compile(r"""(?:src|background|srcset)\s*=\s*["']?\s*(?:https?:)?//|url\(\s*["']?\s*(?:https?:)?//""",
                       re.IGNORECASE)
URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


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
    return bool(body_html and REMOTE_RE.search(body_html))


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


def wrap_html(body: str) -> str:
    return "<meta charset='utf-8'><style>" + _page_style() + "</style>" + body


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
        def acceptNavigationRequest(self, url, nav_type, is_main_frame):
            if url.scheme() in ("simplemail", "about", "data"):
                return True
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                QDesktopServices.openUrl(url)  # ссылки — во внешнем браузере
            return False


class MessageView(QWidget):
    remote_allowed_for_sender = Signal(str)  # пользователь выбрал «всегда показывать» для отправителя
    state_changed = Signal()                 # показано другое письмо / загружены картинки

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
            page = _Page(self.profile, self.web)
            s = page.settings()
            s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
            self.web.setPage(page)
            self.stack.addWidget(self.web)
        else:
            self.web = None
            self.text = QTextBrowser()
            self.text.setOpenExternalLinks(True)
            self.stack.addWidget(self.text)

        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.banner)
        lay.addWidget(holder, 1)
        self._apply_theme()

    # ---------- API ----------
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
