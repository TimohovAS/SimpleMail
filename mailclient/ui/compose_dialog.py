"""Окно написания письма: форматированный текст (HTML) или обычный текст, картинки, вложения, подпись."""
from __future__ import annotations

from ..i18n import tr

import html as html_lib
import re
import json
import base64
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QImage, QKeySequence, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QToolBar, QVBoxLayout,
)

from ..db import Database
from ..protocols import build_message
from ..delivery import deliver_outbox
from .editor import RichEditor
from .icons import icon
from .messages import show_error
from .worker import run_task

SIG_SEPARATOR = "-- "
QUOTE_STYLE = "margin-left:6px; padding-left:8px; border-left:2px solid #9ca3af;"


def body_inner(html: str) -> str:
    """Содержимое <body> (подпись хранится целым HTML-документом Qt)."""
    m = re.search(r"<body[^>]*>(.*)</body>", html or "", re.S | re.I)
    return m.group(1) if m else (html or "")


def signature_block(sig_html: str) -> str:
    inner = body_inner(sig_html).strip()
    return f"<p>{SIG_SEPARATOR}</p>{inner}" if inner else ""


def quote_html(header: str, text: str) -> str:
    body = "<br>".join(html_lib.escape(line) for line in (text or "").splitlines())
    return (f"<p>{html_lib.escape(header)}</p>"
            f"<blockquote style='{QUOTE_STYLE}'>{body}</blockquote>")


class ComposeDialog(QDialog):
    sent = Signal()
    saved = Signal()

    def __init__(self, db: Database, account_id: int | None = None, to: str = "", subject: str = "",
                 cc: str = "", body: str = "", in_reply_to: str = "", quote_header: str = "",
                 quote_text: str = "", attachments: list[tuple[str, bytes]] | None = None, parent=None, draft_id=None):
        super().__init__(parent)
        self.db = db
        self._draft_id = draft_id
        self._dirty = False
        loaded = None
        if draft_id is not None:
            loaded = db.message(draft_id)
            if not loaded or loaded.folder not in ("drafts", "outbox"):
                raise ValueError("This composition is no longer editable")
            if loaded.delivery_state in ("sending", "smtp_sent", "append_pending"):
                raise ValueError("This message is being delivered or has already been sent")
            state = json.loads(loaded.compose_state or "{}")
            account_id, to, cc, subject = loaded.account_id, state.get("to", ""), state.get("cc", ""), loaded.subject
            in_reply_to, quote_header = state.get("in_reply_to", ""), state.get("quote_header", "")
            attachments = db.message_attachments(draft_id)
            # При редактировании очередь больше не может отправить старую версию.
            db.update_message(draft_id, folder="drafts", delivery_state=None, delivery_error=None)
        self.in_reply_to = in_reply_to
        self.quote_header = quote_header
        self.attachments: list[tuple[str, bytes]] = list(attachments or [])
        self._sending = False
        self.setWindowTitle(subject or tr("Новое письмо"))
        self.setWindowFlags(Qt.Window)  # обычное окно: можно свернуть / развернуть
        self.resize(900, 700)

        # ---- верхняя панель ----
        tb = QToolBar(); tb.setIconSize(QSize(22, 22)); tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.send_act = QAction(icon("send"), tr("Отправить"), self)
        self.send_act.setShortcut(QKeySequence("Ctrl+Return"))
        self.send_act.setToolTip(tr("Отправить (Ctrl+Enter)"))
        self.send_act.triggered.connect(self._send)
        attach = QAction(icon("paperclip"), tr("Вложить файл"), self)
        attach.setShortcut(QKeySequence("Ctrl+Shift+A"))
        attach.setToolTip(tr("Вложить файлы (Ctrl+Shift+A)"))
        attach.triggered.connect(self._attach)
        tb.addAction(self.send_act); tb.addAction(attach)
        discard = QAction(tr("Удалить черновик"), self)
        discard.triggered.connect(self._discard)
        tb.addAction(discard)
        tb.addSeparator()
        tb.addWidget(QLabel(tr(" Формат: ")))
        self.mode = QComboBox()
        self.mode.addItem(tr("С оформлением (HTML)"), "html")
        self.mode.addItem(tr("Обычный текст"), "plain")
        self.mode.setToolTip(tr("Обычный текст — без шрифтов, цветов и картинок"))
        self.mode.currentIndexChanged.connect(self._mode_changed)
        tb.addWidget(self.mode)
        tb.addWidget(QLabel(tr("  Подпись: ")))
        self.sig = QComboBox()
        self.sig.setToolTip(tr("Подпись в конце письма. По умолчанию — та, что выбрана в настройках ящика"))
        self.sig.addItem(tr("Без подписи"), None)
        for s in db.signatures():
            self.sig.addItem(s.name, s.id)
        tb.addWidget(self.sig)

        # ---- поля ----
        self.account = QComboBox()
        for a in db.accounts():
            self.account.addItem(a.email if a.name in ("", a.email) else f"{a.name} <{a.email}>", a.id)
            if a.id == account_id:
                self.account.setCurrentIndex(self.account.count() - 1)
        self._select_account_signature()
        self.sig.currentIndexChanged.connect(self._signature_changed)
        self.account.currentIndexChanged.connect(self._account_changed)
        self.to = QLineEdit(to)
        self.cc = QLineEdit(cc)
        self.subject = QLineEdit(subject)
        self.subject.textChanged.connect(lambda t: self.setWindowTitle(t or tr("Новое письмо")))
        form = QFormLayout()
        form.addRow(tr("От:"), self.account)
        form.addRow(tr("Кому:"), self.to)
        form.addRow(tr("Копия:"), self.cc)
        form.addRow(tr("Тема:"), self.subject)

        self.att_list = QListWidget()
        self.att_list.setFlow(QListWidget.LeftToRight)
        self.att_list.setMaximumHeight(34)
        self.att_list.setToolTip(tr("Вложения. Delete — убрать выбранное"))
        self.att_list.hide()

        # ---- текст ----
        self.editor = RichEditor()
        lines = "".join(f"<p>{html_lib.escape(l) or '<br>'}</p>" for l in body.splitlines()) if body else ""
        quote = quote_html(quote_header, quote_text) if quote_header else ""
        self.editor.set_html("<p><br></p>" + lines + "<p><br></p>" + signature_block(self._signature()) + quote)
        if loaded:
            self.editor.set_html(state.get("html", loaded.body_html or loaded.body_text))
            self.editor._images = {cid: (mime, base64.b64decode(data)) for cid, mime, data in state.get("inline_images", [])}
            for cid, (_, data) in self.editor._images.items():
                self.editor.edit.document().addResource(QTextDocument.ImageResource, QUrl("cid:" + cid), QImage.fromData(data))
            self.mode.setCurrentIndex(0 if state.get("mode", "html") == "html" else 1)
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
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(1000)
        self._autosave.timeout.connect(self._save_draft)
        for field in (self.to, self.cc, self.subject):
            field.textChanged.connect(self._schedule_save)
        self.editor.edit.textChanged.connect(self._schedule_save)
        self.account.currentIndexChanged.connect(self._schedule_save)
        self.mode.currentIndexChanged.connect(self._schedule_save)

    def _schedule_save(self, *_):
        self._dirty = True
        if not self._sending:
            self._autosave.start()

    def _composition(self):
        acc = self.db.account(self.account.currentData())
        if not acc:
            return None, None
        state = dict(to=self.to.text().strip(), cc=self.cc.text().strip(), html=self.editor.to_html(),
                     mode=self.mode.currentData(), in_reply_to=self.in_reply_to, quote_header=self.quote_header,
                     inline_images=[(cid, mime, base64.b64encode(data).decode()) for cid, mime, data in self.editor.inline_images()])
        em = build_message(acc, state["to"], self.subject.text(), self.editor.to_plain(), cc=state["cc"],
                           in_reply_to=self.in_reply_to, attachments=self.attachments,
                           html=state["html"] if state["mode"] == "html" else None,
                           inline_images=self.editor.inline_images() if state["mode"] == "html" else None)
        return state, em

    def _save_draft(self, queued=False):
        if self._sending or not self._dirty and not queued:
            return
        state, em = self._composition()
        if em is None:
            return
        try:
            self._draft_id = self.db.save_composition(self.account.currentData(), em.as_bytes(), state, self._draft_id, queued)
        except Exception as error:
            if queued:
                raise
            self.setWindowTitle(tr("Не удалось сохранить черновик: {0}", str(error)))
            return
        self._dirty = False
        self.saved.emit()

    def _discard(self):
        if self._sending:
            return
        self._autosave.stop()
        if self._draft_id is not None:
            self.db.delete_message(self._draft_id)
        self._dirty = False
        self.saved.emit()
        self.accept()

    # ---------- подпись ----------
    def _signature(self) -> str:
        sig = self.db.signature(self.sig.currentData())
        return sig.html if sig else ""

    def _select_account_signature(self):
        aid = self.account.currentData()
        acc = self.db.account(aid) if aid else None
        i = self.sig.findData(acc.signature_id) if acc and acc.signature_id else 0
        self.sig.setCurrentIndex(max(i, 0))

    def _account_changed(self):
        """Сменили ящик — ставим его подпись по умолчанию."""
        self.sig.blockSignals(True)
        self._select_account_signature()
        self.sig.blockSignals(False)
        self._signature_changed()

    def _signature_blocks(self):
        """(блок «-- » или None, блок заголовка цитаты или None) — ищем только до цитаты,
        чтобы не задеть подпись в цитируемом письме."""
        doc = self.editor.edit.document()
        sep = quote = None
        b = doc.begin()
        while b.isValid():
            text = b.text()
            if self.quote_header and text == self.quote_header:
                quote = b
                break
            if sep is None and text.rstrip() == SIG_SEPARATOR.rstrip():
                sep = b
            b = b.next()
        return sep, quote

    def _signature_changed(self):
        """Заменяем блок подписи (от «-- » до цитаты или конца письма)."""
        doc = self.editor.edit.document()
        unchanged = self.editor.to_plain() == self._initial_text
        new_block = signature_block(self._signature())
        sep, quote = self._signature_blocks()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        if sep is not None:  # убираем старую подпись целиком, вместе с её абзацами
            if quote is not None:
                cur.setPosition(sep.position())
                cur.setPosition(quote.position(), QTextCursor.KeepAnchor)
            else:
                cur.setPosition(max(sep.position() - 1, 0))
                cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            cur.removeSelectedText()
        if new_block:
            if quote is not None:  # новый пустой абзац перед цитатой
                _, quote = self._signature_blocks()
                cur.setPosition(quote.position())
                cur.insertBlock()
                cur.movePosition(QTextCursor.PreviousBlock)
            else:
                cur.movePosition(QTextCursor.End)
                cur.insertBlock()
            cur.setBlockFormat(QTextBlockFormat())
            cur.setCharFormat(QTextCharFormat())
            if self.mode.currentData() == "plain":
                cur.insertText(RichEditor.plain_from_html(new_block))
            else:
                cur.insertHtml(new_block)
        cur.endEditBlock()
        if unchanged:  # смена подписи сама по себе не считается правкой письма
            self._initial_text = self.editor.to_plain()

    # ---------- формат ----------
    def _mode_changed(self):
        plain = self.mode.currentData() == "plain"
        if plain:
            if self.editor.inline_images() and QMessageBox.question(
                    self, tr("Обычный текст"), tr("Оформление и картинки в тексте будут потеряны. Продолжить?")) != QMessageBox.Yes:
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
        files, _ = QFileDialog.getOpenFileNames(self, tr("Вложить файлы"))
        for f in files:
            self.attachments.append((Path(f).name, Path(f).read_bytes()))
        self._refresh_attachments()

    def _refresh_attachments(self):
        if hasattr(self, "_autosave"):
            self._schedule_save()
        self.att_list.clear()
        for name, data in self.attachments:
            size = len(data)
            human = tr("{0:.1f} МБ", size / 1048576) if size > 1048576 else tr("{0} КБ", max(1, size // 1024))
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
            QMessageBox.warning(self, tr("Отправка"), tr("Сначала добавьте почтовый ящик."))
            return
        if not self.to.text().strip():
            QMessageBox.warning(self, tr("Отправка"), tr("Укажите получателя."))
            self.to.setFocus()
            return
        if not self.subject.text().strip() and \
                QMessageBox.question(self, tr("Отправка"), tr("Отправить письмо без темы?")) != QMessageBox.Yes:
            self.subject.setFocus()
            return
        self._autosave.stop()
        try:
            self._save_draft(queued=True)
        except Exception as error:
            from ..errors import explain
            show_error(self, tr("Не удалось сохранить исходящее письмо"), explain(error))
            return
        self._sending = True
        self.setEnabled(False)
        self.send_act.setEnabled(False)
        self.send_act.setText(tr("Отправляю…"))

        def done(result):
            self._sending = False
            self.setEnabled(True)
            if result == "queued":
                QMessageBox.information(self, tr("Исходящие"), tr("Письмо сохранено в исходящих и будет отправлено при подключении."))
            elif result == "uncertain":
                QMessageBox.warning(self, tr("Исходящие"), tr("Результат отправки неизвестен. Проверьте доставку перед повторной отправкой."))
            self.sent.emit()
            self.accept()

        def fail(err):
            self._sending = False
            self.setEnabled(True)
            self.send_act.setEnabled(True)
            self.send_act.setText(tr("Отправить"))
            show_error(self, tr("Письмо не отправлено"), err)

        run_task(deliver_outbox, self.db, self._draft_id, on_done=done, on_error=fail)

    def reject(self):
        if self._sending:
            return
        self._autosave.stop()
        self._save_draft()
        if self._dirty:
            return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
