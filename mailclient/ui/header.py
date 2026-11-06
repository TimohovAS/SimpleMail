"""Шапка открытого письма: аватар с инициалами, отправитель, тема, получатели, кнопки действий.
И поле быстрого ответа внизу области просмотра."""
from __future__ import annotations

from ..i18n import spam_reason, tr

import hashlib
import html
from datetime import datetime
from email.utils import formataddr, getaddresses, parseaddr

from PySide6.QtCore import QEvent, QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPalette
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .icons import icon

AVATAR_COLORS = ["#16a34a", "#2563eb", "#9333ea", "#db2777", "#ea580c", "#0891b2", "#65a30d", "#4f46e5"]


def initials(name: str, addr: str) -> str:
    words = [w for w in (name or "").replace('"', "").split() if w[:1].isalpha()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return (addr[:2] or "?").upper()


def human_date(ts: int, full: bool = False) -> str:
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    months = [tr("янв"), tr("фев"), tr("мар"), tr("апр"), tr("мая"), tr("июн"), tr("июл"), tr("авг"), tr("сен"), tr("окт"), tr("ноя"), tr("дек")]
    if dt.date() == datetime.now().date() and not full:
        return dt.strftime("%H:%M")
    return f"{dt.day} {months[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def sender_html(name: str, addr: str, muted: str) -> str:
    """Имя жирным и адрес ящика рядом: «sales <sales@example.com>».
    Адрес показываем всегда — по одному имени не видно, с какого ящика пришло письмо."""
    name = (name or "").strip()
    addr = (addr or "").strip()
    if not addr:
        return f"<b>{html.escape(name or tr('(без отправителя)'))}</b>"
    if not name or name.lower() == addr.lower():
        return f"<b>{html.escape(addr)}</b>"
    return (f"<b>{html.escape(name)}</b>&nbsp;"
            f"<span style=\"color:{muted}; font-weight:normal\">&lt;{html.escape(addr)}&gt;</span>")


class Avatar(QWidget):
    def __init__(self, size: int = 44, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.text, self.color = "", QColor("#9ca3af")

    def set_person(self, name: str, addr: str):
        self.text = initials(name, addr)
        h = int(hashlib.md5((addr or name).lower().encode()).hexdigest(), 16)
        self.color = QColor(AVATAR_COLORS[h % len(AVATAR_COLORS)])
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(self.color)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        f = QFont(self.font()); f.setPixelSize(int(self.height() * 0.4)); f.setBold(True)
        p.setFont(f); p.setPen(QColor("white"))
        p.drawText(self.rect(), Qt.AlignCenter, self.text)


class MessageHeader(QWidget):
    reply = Signal()
    reply_all = Signal()
    forward = Signal()
    redirect = Signal()
    save_attachments = Signal()
    compose_to = Signal(str)                 # «Написать письмо» из меню отправителя
    thread_toggled = Signal(bool)            # кнопка «Переписка»: показать всю цепочку писем

    def __init__(self, parent=None):
        super().__init__(parent)
        self.avatar = Avatar(36)
        self.sender = QLabel(); self.sender.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.sender.font(); f.setPointSizeF(f.pointSizeF() * 1.15); self.sender.setFont(f)
        self.sender.setTextFormat(Qt.RichText)
        self.sender.setContextMenuPolicy(Qt.CustomContextMenu)
        self.sender.customContextMenuRequested.connect(self._sender_menu)
        self._sender_name, self._sender_addr = "", ""
        self.time = QLabel(); self.time.setProperty("tone", "muted")
        self.subject = QLabel(); self.subject.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recipients = QLabel(); self.recipients.setTextInteractionFlags(Qt.TextSelectableByMouse)
        for lbl in (self.sender, self.subject, self.recipients):
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # длинный текст не раздувает окно

        self._buttons: list[QToolButton] = []

        def btn(text, icon_name, sig, tip):
            b = QToolButton(); b.setText(text); b.setIcon(icon(icon_name)); b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); b.setAutoRaise(True)
            b.clicked.connect(sig.emit)
            self._buttons.append(b)
            return b

        self.buttons = QHBoxLayout(); self.buttons.setSpacing(0); self.buttons.setContentsMargins(0, 0, 0, 0)
        self.thread_btn = QToolButton(); self.thread_btn.setIcon(icon("messages-square"))
        self.thread_btn.setCheckable(True); self.thread_btn.setAutoRaise(True)
        self.thread_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.thread_btn.toggled.connect(self.thread_toggled.emit)
        self.thread_btn.hide()  # видна, когда у письма есть переписка (больше одного письма)
        self._buttons.append(self.thread_btn)
        self.buttons.addWidget(self.thread_btn)
        self.buttons.addWidget(btn(tr("Ответить"), "reply", self.reply, tr("Ответить отправителю")))
        self.buttons.addWidget(btn(tr("Всем"), "reply-all", self.reply_all, tr("Ответить всем")))
        self.buttons.addWidget(btn(tr("Переслать"), "forward", self.forward, tr("Переслать с вложениями")))
        self.buttons.addWidget(btn(tr("Перенаправить"), "send", self.redirect,
                                   tr("Перенаправить: отправить письмо как есть другому адресату")))
        self._body_buttons = list(self._buttons[1:])
        self.att_btn = btn(tr("Вложения"), "paperclip", self.save_attachments, tr("Сохранить вложения"))
        self.att_btn.hide()  # вложения показывает отдельная панель под шапкой
        self._buttons.remove(self.att_btn)

        # строка 1: отправитель … дата;  строка 2: тема … кнопки;  строка 3: кому
        top = QHBoxLayout(); top.setSpacing(6)
        top.addWidget(self.sender, 1); top.addWidget(self.time, 0, Qt.AlignVCenter)
        mid = QHBoxLayout(); mid.setSpacing(6)
        mid.addWidget(self.subject, 1); mid.addLayout(self.buttons)
        text = QVBoxLayout(); text.setSpacing(0)
        text.addLayout(top); text.addLayout(mid); text.addWidget(self.recipients)
        row = QHBoxLayout(); row.setSpacing(8)
        row.addWidget(self.avatar, 0, Qt.AlignTop); row.addLayout(text, 1)
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 4, 6, 2)
        lay.addLayout(row)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.hide()

    def resizeEvent(self, e):
        """В узком окне у кнопок остаются только значки."""
        super().resizeEvent(e)
        style = Qt.ToolButtonIconOnly if self.width() < 760 else Qt.ToolButtonTextBesideIcon
        for b in self._buttons:
            b.setToolButtonStyle(style)

    def show_message(self, m, attachment_names: list[str]):
        body_ready = bool(getattr(m, "body_downloaded", True))
        for button in self._body_buttons:
            button.setEnabled(body_ready)
        self.att_btn.setEnabled(body_ready)
        name, addr = parseaddr(m.sender or "")
        if not addr and "@" not in (m.sender or ""):
            name, addr = (m.sender or "").strip(), ""
        self._sender_name, self._sender_addr = name, addr
        self.avatar.set_person(name, addr)
        self._render_sender()
        self.sender.setToolTip(tr("{0}\nПравая кнопка мыши — скопировать адрес", m.sender) if addr else (m.sender or ""))
        self.time.setText(human_date(m.date_ts, full=True))
        self.subject.setText(html.escape(m.subject or tr("(без темы)")))
        self.subject.setToolTip(m.subject or "")
        rcpt = ", ".join(n or a for n, a in getaddresses([m.recipients or ""]) if n or a)
        line = tr("<b>Кому:</b> {0}", html.escape(rcpt))
        if m.spam_reason:
            line += tr(" <span>&nbsp;·&nbsp; Спам-фильтр: {0}</span>", html.escape(spam_reason(m.spam_reason)))
        self.recipients.setText(line)
        self.recipients.setToolTip(m.recipients)
        self.show()

    def set_thread_count(self, count: int):
        """Сколько писем в переписке открытого письма (кнопка видна, если их больше одного)."""
        self.thread_btn.setText(tr("Переписка ({0})", count))
        self.thread_btn.setToolTip(tr("Показать всю переписку (писем: {0}) или только это письмо (Ctrl+T)", count))
        self.thread_btn.setVisible(count > 1)

    def set_thread_checked(self, on: bool):
        with QSignalBlocker(self.thread_btn):
            self.thread_btn.setChecked(on)

    def _render_sender(self):
        muted = QGuiApplication.palette().color(QPalette.PlaceholderText).name()
        self.sender.setText(sender_html(self._sender_name, self._sender_addr, muted))

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.PaletteChange and self._sender_addr:
            self._render_sender()  # светлая/тёмная тема: цвет адреса следом

    def sender_menu(self) -> QMenu:
        menu = QMenu(self)
        name, addr = self._sender_name, self._sender_addr
        if addr:
            menu.addAction(tr("Копировать адрес"), lambda: QGuiApplication.clipboard().setText(addr))
            if name and name.lower() != addr.lower():
                full = formataddr((name, addr))
                menu.addAction(tr("Копировать имя и адрес"), lambda: QGuiApplication.clipboard().setText(full))
            menu.addAction(tr("Написать письмо"), lambda: self.compose_to.emit(formataddr((name, addr))))
        if self.sender.hasSelectedText():
            menu.addSeparator()
            menu.addAction(tr("Копировать выделенное"),
                           lambda: QGuiApplication.clipboard().setText(self.sender.selectedText()))
        return menu

    def _sender_menu(self, pos):
        menu = self.sender_menu()
        if not menu.isEmpty():
            menu.exec(self.sender.mapToGlobal(pos))


class QuickReply(QWidget):
    """Поле быстрого ответа: текст уходит ответом отправителю с цитатой и подписью."""
    send = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(tr("Быстрый ответ… (Ctrl+Enter — отправить)"))
        self.edit.setMinimumHeight(40)
        self.btn = QPushButton(icon("send"), "")
        self.btn.setToolTip(tr("Отправить ответ (Ctrl+Enter)"))
        self.btn.setFixedSize(40, 40)
        self.btn.clicked.connect(self._send)
        lay = QHBoxLayout(self); lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.edit, 1); lay.addWidget(self.btn, 0, Qt.AlignVCenter)
        self.edit.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and ev.modifiers() & Qt.ControlModifier:
            self._send()
            return True
        return super().eventFilter(obj, ev)

    def _send(self):
        text = self.edit.toPlainText().strip()
        if text:
            self.send.emit(text)

    def set_busy(self, busy: bool):
        self.edit.setReadOnly(busy)
        self.btn.setEnabled(not busy)

    def clear(self):
        self.edit.clear()
