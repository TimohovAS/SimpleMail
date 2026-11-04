"""Окно написания письма: форматированный текст (HTML) или обычный текст, картинки, вложения, подпись."""
from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QToolBar, QVBoxLayout,
)

from ..db import Database
from ..protocols import build_message, send_message
from .editor import RichEditor
from .icons import icon
from .messages import show_error
from .worker import run_task

SIG_SEPARATOR = "-- "
QUOTE_STYLE = "margin-left:6px; padding-left:8px; border-left:2px solid #9ca3af; color:#4b5563;"


def body_inner(html: str) -> str:
    """Содержимое <body> (подпись хранится целым HTML-документом Qt)."""
    m = re.search(r"<body[^>]*>(.*)</body>", html or "", re.S | re.I)
    return m.group(1) if m else (html or "")


def signature_block(sig_html: str) -> str:
    inner = body_inner(sig_html).strip()
    return f"<p>{SIG_SEPARATOR}</p>{inner}" if inner else ""


def quote_html(header: str, text: str) -> str:
    body = "<br>".join(html_lib.escape(line) for line in (text or "").splitlines())
    return (f"<p style='color:#4b5563'>{html_lib.escape(header)}</p>"
            f"<blockquote style='{QUOTE_STYLE}'>{body}</blockquote>")


class ComposeDialog(QDialog):
    sent = Signal()

    def __init__(self, db: Database, account_id: int | None = None, to: str = "", subject: str = "",
                 cc: str = "", body: str = "", in_reply_to: str = "", quote_header: str = "",
                 quote_text: str = "", attachments: list[tuple[str, bytes]] | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.in_reply_to = in_reply_to
        self.quote_header = quote_header
        self.attachments: list[tuple[str, bytes]] = list(attachments or [])
        self._sending = False
        self.setWindowTitle(subject or "Новое письмо")
        self.setWindowFlags(Qt.Window)  # обычное окно: можно свернуть / развернуть
        self.resize(900, 700)

        # ---- верхняя панель ----
        tb = QToolBar(); tb.setIconSize(QSize(22, 22)); tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.send_act = QAction(icon("send"), "Отправить", self)
        self.send_act.setShortcut(QKeySequence("Ctrl+Return"))
        self.send_act.setToolTip("Отправить (Ctrl+Enter)")
        self.send_act.triggered.connect(self._send)
        attach = QAction(icon("paperclip"), "Вложить файл", self)
        attach.setShortcut(QKeySequence("Ctrl+Shift+A"))
        attach.setToolTip("Вложить файлы (Ctrl+Shift+A)")
        attach.triggered.connect(self._attach)
        tb.addAction(self.send_act); tb.addAction(attach)
        tb.addSeparator()
        tb.addWidget(QLabel(" Формат: "))
        self.mode = QComboBox()
        self.mode.addItem("С оформлением (HTML)", "html")
        self.mode.addItem("Обычный текст", "plain")
        self.mode.setToolTip("Обычный текст — без шрифтов, цветов и картинок")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        tb.addWidget(self.mode)

        # ---- поля ----
        self.account = QComboBox()
        for a in db.accounts():
            self.account.addItem(a.email if a.name in ("", a.email) else f"{a.name} <{a.email}>", a.id)
            if a.id == account_id:
                self.account.setCurrentIndex(self.account.count() - 1)
        self.account.currentIndexChanged.connect(self._account_changed)
        self.to = QLineEdit(to)
        self.cc = QLineEdit(cc)
        self.subject = QLineEdit(subject)
        self.subject.textChanged.connect(lambda t: self.setWindowTitle(t or "Новое письмо"))
        form = QFormLayout()
        form.addRow("От:", self.account)
        form.addRow("Кому:", self.to)
        form.addRow("Копия:", self.cc)
        form.addRow("Тема:", self.subject)

        self.att_list = QListWidget()
        self.att_list.setFlow(QListWidget.LeftToRight)
        self.att_list.setMaximumHeight(34)
        self.att_list.setToolTip("Вложения. Delete — убрать выбранное")
        self.att_list.hide()

        # ---- текст ----
        self.editor = RichEditor()
        lines = "".join(f"<p>{html_lib.escape(l) or '<br>'}</p>" for l in body.splitlines()) if body else ""
        quote = quote_html(quote_header, quote_text) if quote_header else ""
        self.editor.set_html("<p><br></p>" + lines + "<p><br></p>" + signature_block(self._signature()) + quote)
        self._initial_text = self.editor.to_plain()
        self._initial_att = len(self.attachments)
        self.editor.edit.moveCursor(QTextCursor.Start)

        self._refresh_attachments()
        lay = QVBoxLayout(self); lay.setContentsMargins(6, 0, 6, 6)
        lay.addWidget(tb)
        lay.addLayout(form)
        lay.addWidget(self.att_list)
        lay.addWidget(self.editor, 1)
        (self.editor.edit if to else self.to).setFocus()

    # ---------- подпись ----------
    def _signature(self) -> str:
        aid = self.account.currentData()
        acc = self.db.account(aid) if aid else None
        return acc.signature if acc else ""

    def _account_changed(self):
        """Сменили ящик — заменяем подпись (блок от «-- » до цитаты или конца письма)."""
        doc = self.editor.edit.document()
        start = doc.find(SIG_SEPARATOR)
        new_block = signature_block(self._signature())
        cur = QTextCursor(doc)
        if start.isNull():
            if not new_block:
                return
            end = doc.find(self.quote_header) if self.quote_header else QTextCursor()
            cur.setPosition(end.selectionStart() if not end.isNull() else doc.characterCount() - 1)
        else:
            cur.setPosition(start.block().position())
            end = doc.find(self.quote_header, start) if self.quote_header else QTextCursor()
            stop = end.block().position() - 1 if not end.isNull() else doc.characterCount() - 1
            cur.setPosition(max(stop, cur.position()), QTextCursor.KeepAnchor)
            cur.removeSelectedText()
        if self.mode.currentData() == "plain":
            cur.insertText(RichEditor.plain_from_html(new_block))
        else:
            cur.insertHtml(new_block)
        self._initial_text = self.editor.to_plain()

    # ---------- формат ----------
    def _mode_changed(self):
        plain = self.mode.currentData() == "plain"
        if plain:
            if self.editor.inline_images() and QMessageBox.question(
                    self, "Обычный текст", "Оформление и картинки в тексте будут потеряны. Продолжить?") != QMessageBox.Yes:
                self.mode.blockSignals(True); self.mode.setCurrentIndex(0); self.mode.blockSignals(False)
                return
            text = self.editor.to_plain()
            self.editor.edit.setAcceptRichText(False)
            self.editor.edit.setPlainText(text)
        else:
            self.editor.edit.setAcceptRichText(True)
        self.editor.set_formatting_visible(not plain)

    # ---------- вложения ----------
    def _attach(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Вложить файлы")
        for f in files:
            self.attachments.append((Path(f).name, Path(f).read_bytes()))
        self._refresh_attachments()

    def _refresh_attachments(self):
        self.att_list.clear()
        for name, data in self.attachments:
            size = len(data)
            human = f"{size / 1048576:.1f} МБ" if size > 1048576 else f"{max(1, size // 1024)} КБ"
            self.att_list.addItem(QListWidgetItem(icon("paperclip"), f"{name} ({human})"))
        self.att_list.setVisible(bool(self.attachments))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.att_list.hasFocus() and self.att_list.currentRow() >= 0:
            del self.attachments[self.att_list.currentRow()]
            self._refresh_attachments()
            return
        super().keyPressEvent(event)

    # ---------- отправка ----------
    def _send(self):
        aid = self.account.currentData()
        if aid is None:
            QMessageBox.warning(self, "Отправка", "Сначала добавьте почтовый ящик.")
            return
        if not self.to.text().strip():
            QMessageBox.warning(self, "Отправка", "Укажите получателя.")
            self.to.setFocus()
            return
        if not self.subject.text().strip() and \
                QMessageBox.question(self, "Отправка", "Отправить письмо без темы?") != QMessageBox.Yes:
            self.subject.setFocus()
            return
        acc = self.db.account(aid)
        is_html = self.mode.currentData() == "html"
        em = build_message(acc, self.to.text().strip(), self.subject.text(), self.editor.to_plain(),
                           cc=self.cc.text().strip(), in_reply_to=self.in_reply_to, attachments=self.attachments,
                           html=self.editor.to_html() if is_html else None,
                           inline_images=self.editor.inline_images() if is_html else None)
        self._sending = True
        self.send_act.setEnabled(False)
        self.send_act.setText("Отправляю…")

        def done(_):
            self._sending = False
            self.sent.emit()
            self.accept()

        def fail(err):
            self._sending = False
            self.send_act.setEnabled(True)
            self.send_act.setText("Отправить")
            show_error(self, "Письмо не отправлено", err)

        run_task(send_message, self.db, acc, em, on_done=done, on_error=fail)

    def reject(self):
        if self._sending:
            return
        if (self.editor.to_plain() != self._initial_text or len(self.attachments) > self._initial_att) and QMessageBox.question(
                self, "Закрыть письмо", "Закрыть письмо без отправки? Текст будет потерян.") != QMessageBox.Yes:
            return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
