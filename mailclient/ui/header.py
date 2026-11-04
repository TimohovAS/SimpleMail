"""Шапка открытого письма: аватар с инициалами, отправитель, тема, получатели, кнопки действий.
И поле быстрого ответа внизу области просмотра."""
from __future__ import annotations

import hashlib
import html
from datetime import datetime
from email.utils import getaddresses, parseaddr

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
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
    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    if dt.date() == datetime.now().date() and not full:
        return dt.strftime("%H:%M")
    return f"{dt.day} {months[dt.month - 1]} {dt.year}, {dt:%H:%M}"


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.avatar = Avatar()
        self.sender = QLabel(); self.sender.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.sender.font(); f.setPointSizeF(f.pointSizeF() * 1.25); f.setBold(True); self.sender.setFont(f)
        self.time = QLabel(); self.time.setStyleSheet("color:#6b7280")
        self.subject = QLabel(); self.subject.setWordWrap(True); self.subject.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recipients = QLabel(); self.recipients.setWordWrap(True); self.recipients.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recipients.setStyleSheet("color:#374151")
        self.extra = QLabel(); self.extra.setWordWrap(True); self.extra.setStyleSheet("color:#6b7280")

        def btn(text, icon_name, sig, tip):
            b = QToolButton(); b.setText(text); b.setIcon(icon(icon_name)); b.setToolTip(tip)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); b.setAutoRaise(False)
            b.clicked.connect(sig.emit)
            return b

        self.buttons = QHBoxLayout(); self.buttons.setSpacing(4)
        self.buttons.addWidget(btn("Ответить", "reply", self.reply, "Ответить отправителю"))
        self.buttons.addWidget(btn("Всем", "reply-all", self.reply_all, "Ответить всем"))
        self.buttons.addWidget(btn("Переслать", "forward", self.forward, "Переслать с вложениями"))
        self.buttons.addWidget(btn("Перенаправить", "send", self.redirect,
                                   "Перенаправить: отправить письмо как есть другому адресату"))
        self.att_btn = btn("Вложения", "paperclip", self.save_attachments, "Сохранить вложения")
        self.buttons.addWidget(self.att_btn)
        self.buttons.addStretch(1)

        top = QHBoxLayout(); top.addWidget(self.sender, 1); top.addWidget(self.time, 0, Qt.AlignTop)
        text = QVBoxLayout(); text.setSpacing(2)
        text.addLayout(top); text.addWidget(self.subject); text.addWidget(self.recipients); text.addWidget(self.extra)
        row = QHBoxLayout(); row.addWidget(self.avatar, 0, Qt.AlignTop); row.addLayout(text, 1)
        lay = QVBoxLayout(self); lay.setContentsMargins(8, 8, 8, 4)
        lay.addLayout(row); lay.addLayout(self.buttons)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.hide()

    def show_message(self, m, attachment_names: list[str]):
        name, addr = parseaddr(m.sender or "")
        self.avatar.set_person(name, addr)
        self.sender.setText(html.escape(name or addr or "(без отправителя)"))
        self.sender.setToolTip(m.sender)
        self.time.setText(human_date(m.date_ts, full=True))
        self.subject.setText(html.escape(m.subject or "(без темы)"))
        rcpt = ", ".join(n or a for n, a in getaddresses([m.recipients or ""]) if n or a)
        self.recipients.setText(f"<b>Кому:</b> {html.escape(rcpt)}")
        self.recipients.setToolTip(m.recipients)
        extra = []
        if attachment_names:
            extra.append("Вложения: " + ", ".join(attachment_names))
        if m.spam_reason:
            extra.append("Спам-фильтр: " + m.spam_reason)
        self.extra.setText(html.escape(" · ".join(extra)))
        self.extra.setVisible(bool(extra))
        self.att_btn.setVisible(bool(attachment_names))
        self.show()


class QuickReply(QWidget):
    """Поле быстрого ответа: текст уходит ответом отправителю с цитатой и подписью."""
    send = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("Быстрый ответ… (Ctrl+Enter — отправить)")
        self.edit.setFixedHeight(54)
        self.btn = QPushButton(icon("send"), "")
        self.btn.setToolTip("Отправить ответ (Ctrl+Enter)")
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
