"""Окно написания письма: форматированный текст (HTML) или обычный текст, картинки, вложения, подпись."""
from __future__ import annotations

from ..i18n import tr

import html as html_lib
import re
import json
import base64
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QAction, QKeySequence, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from ..db import MARK_ANSWERED, Database
from ..protocols import build_message
from ..delivery import deliver_outbox
from .address_book import AddressBookDialog, AddressEdit
from .editor import QUOTE_BODY_HTML, QUOTE_OPEN_HTML, RichEditor
from .file_drop import ZONE_ATTACH, ZONE_INLINE, DropOverlay, all_inline_images, dropped_files, image_type
from .icons import icon
from .messages import show_error
from .worker import run_task
from .image_resources import extract_inline_images

SIG_SEPARATOR = "-- "
DRAG_EVENTS = (QEvent.DragEnter, QEvent.DragMove, QEvent.DragLeave, QEvent.Drop)
QUOTE_STYLE = "margin-left:6px; padding-left:8px; border-left:2px solid #9ca3af;"


def body_inner(html: str) -> str:
    """Содержимое <body> (подпись хранится целым HTML-документом Qt)."""
    m = re.search(r"<body[^>]*>(.*)</body>", html or "", re.S | re.I)
    return m.group(1) if m else (html or "")


def signature_block(sig_html: str) -> str:
    inner = body_inner(sig_html).strip()
    return f"<p>{SIG_SEPARATOR}</p>{inner}" if inner else ""


def quote_html(header: str, text: str, rich_html="", reply=False) -> str:
    """Цитата исходного письма. reply — ответ: стандартная разметка цитаты (как у Gmail),
    чтобы почтовые программы сворачивали её; пересылаемое письмо не сворачивается."""
    from .viewer import sanitize_html
    body = body_inner(sanitize_html(rich_html, local_images_only=True)) if rich_html else "<br>".join(html_lib.escape(line) for line in (text or "").splitlines())
    head = "<br>".join(html_lib.escape(line) for line in header.splitlines())  # шапка пересылки — по строкам
    if reply:
        return f"{QUOTE_OPEN_HTML}<p>{head}</p>{QUOTE_BODY_HTML}{body}</blockquote></div>"
    return (f"<p>{head}</p>"
            f"<blockquote style='{QUOTE_STYLE}'>{body}</blockquote>")


class ComposeDialog(QDialog):
    sent = Signal()
    saved = Signal()

    def __init__(self, db: Database, account_id: int | None = None, to: str = "", subject: str = "",
                 cc: str = "", body: str = "", bcc: str = "", in_reply_to: str = "", quote_header: str = "", references: str = "",
                 quote_text: str = "", attachments: list[tuple[str, bytes]] | None = None, parent=None, draft_id=None,
                 quote_body_html="", quote_images=None, mark_source=None):
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
            references = state.get("references", "")
            bcc = state.get("bcc", "")
            mark_source = state.get("mark_source")
            attachments = db.message_attachments(draft_id)
            # При редактировании очередь больше не может отправить старую версию.
            db.update_message(draft_id, folder="drafts", delivery_state=None, delivery_error=None)
        self.in_reply_to = in_reply_to
        self.references = references
        # [id исходного письма, отметка MARK_*]: после отправки письмо получит значок «ответили»/«переслали».
        self.mark_source = list(mark_source) if mark_source else None
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
        attach.setToolTip(tr("Вложить файлы (Ctrl+Shift+A) — или перетащите их в окно письма"))
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
        if loaded and "signature_id" in state:
            self.sig.setCurrentIndex(max(0, self.sig.findData(state["signature_id"])))
        self.sig.currentIndexChanged.connect(self._signature_changed)
        self.account.currentIndexChanged.connect(self._account_changed)
        self.to = AddressEdit(db, to)
        self.cc = AddressEdit(db, cc)
        self.bcc = AddressEdit(db, bcc)
        self.bcc.setToolTip(tr("Скрытая копия: эти адресаты получат письмо, но остальные их не увидят"))
        self.subject = QLineEdit(subject)
        self.subject.textChanged.connect(lambda t: self.setWindowTitle(t or tr("Новое письмо")))
        form = QFormLayout()
        form.addRow(tr("От:"), self.account)
        form.addRow(tr("Кому:"), self.to)
        form.addRow(tr("Копия:"), self.cc)
        self.bcc_label = QLabel(tr("Скрытая:"))
        form.addRow(self.bcc_label, self.bcc)
        self.bcc_toggle = QAction(tr("Скрытая копия"), self)
        self.bcc_toggle.setCheckable(True)
        self.bcc_toggle.setToolTip(tr("Показать поле «Скрытая копия» (Bcc)"))
        self.bcc_toggle.toggled.connect(self._show_bcc)
        tb.insertAction(discard, self.bcc_toggle)
        book = QAction(icon("book-user"), tr("Адреса"), self)
        book.setShortcut(QKeySequence("Ctrl+Shift+B"))
        book.setToolTip(tr("Выбрать адресатов из адресной книги (Ctrl+Shift+B)"))
        book.triggered.connect(self._address_book)
        tb.insertAction(self.bcc_toggle, book)
        self.bcc_toggle.setChecked(bool(bcc))
        self._show_bcc(bool(bcc))
        form.addRow(tr("Тема:"), self.subject)

        self.att_list = QListWidget()
        self.att_list.setFlow(QListWidget.LeftToRight)
        self.att_list.setMaximumHeight(34)
        self.att_list.setToolTip(tr("Вложения: × или правая кнопка — убрать, Delete — убрать выбранное. "
                                    "Файлы можно перетащить в окно письма"))
        self.att_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.att_list.customContextMenuRequested.connect(self._attachment_menu)
        self.att_list.hide()

        # ---- текст ----
        self.editor = RichEditor()
        if loaded:
            images = [(cid, mime, base64.b64decode(data)) for cid, mime, data in state.get("inline_images", [])]
            self.editor.set_html(state.get("html", loaded.body_html or loaded.body_text), images=images)
            self.mode.setCurrentIndex(0 if state.get("mode", "html") == "html" else 1)
        else:
            lines = "".join(f"<p>{html_lib.escape(l) or '<br>'}</p>" for l in body.splitlines()) if body else ""
            quote = quote_html(quote_header, quote_text, quote_body_html) if quote_header else ""
            signature, images = extract_inline_images(self._signature())
            self.editor.set_html("<p><br></p>" + lines + "<p><br></p>" + signature_block(signature) + quote,
                                 images=[*(quote_images or []), *images])
            if quote and self.mark_source and self.mark_source[1] == MARK_ANSWERED:
                _, header_block = self._signature_blocks()
                if header_block is not None:
                    self.editor.mark_quote(header_block)  # при отправке цитата получит разметку цитаты
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
        for field in (self.to, self.cc, self.bcc, self.subject):
            field.textChanged.connect(self._schedule_save)
        self.editor.edit.textChanged.connect(self._schedule_save)
        self.account.currentIndexChanged.connect(self._schedule_save)
        self.mode.currentIndexChanged.connect(self._schedule_save)
        self._setup_file_drop()

    def _show_bcc(self, visible: bool):
        self.bcc_label.setVisible(visible)
        self.bcc.setVisible(visible)
        if visible and self.isVisible():
            self.bcc.setFocus()

    def _schedule_save(self, *_):
        self._dirty = True
        if not self._sending:
            self._autosave.start()

    def _composition(self):
        acc = self.db.account(self.account.currentData())
        if not acc:
            return None, None
        state = dict(to=self.to.addresses_text(), cc=self.cc.addresses_text(), bcc=self.bcc.addresses_text(), html=self.editor.to_html(),
                     mode=self.mode.currentData(), in_reply_to=self.in_reply_to, references=self.references,
                     quote_header=self.quote_header, mark_source=self.mark_source, signature_id=self.sig.currentData(),
                     inline_images=[(cid, mime, base64.b64encode(data).decode()) for cid, mime, data in self.editor.inline_images()])
        em = build_message(acc, state["to"], self.subject.text(), self.editor.mail_plain(), cc=state["cc"], bcc=state["bcc"],
                           in_reply_to=self.in_reply_to, references=self.references, attachments=self.attachments,
                           html=self.editor.mail_html() if state["mode"] == "html" else None,
                           inline_images=self.editor.inline_images() if state["mode"] == "html" else None)
        return state, em

    def _save_draft(self, queued=False):
        if self._sending or not self._dirty and not queued:
            return
        try:
            state, em = self._composition()
            if em is None:
                return
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

    # ---------- адресная книга ----------
    def _address_book(self):
        dlg = AddressBookDialog(self.db, picker=True, parent=self)
        fields = {"to": self.to, "cc": self.cc, "bcc": self.bcc}

        def picked(field, addresses):
            if field == "bcc" and not self.bcc_toggle.isChecked():
                self.bcc_toggle.setChecked(True)
            fields[field].add_addresses(addresses)
        dlg.picked.connect(picked)
        dlg.exec()

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
            # <br> внутри абзаца Qt хранит как U+2028
            if self.quote_header and text.replace("\u2028", "\n") == self.quote_header:
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
        signature = self._signature()
        plain = self.mode.currentData() == "plain"
        if plain:
            text = RichEditor.plain_from_html(signature).strip()
            new_block = f"{SIG_SEPARATOR}\n{text}" if text else ""
        else:
            signature, images = extract_inline_images(signature)
            self.editor.add_inline_images(images)
            new_block = signature_block(signature)
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
            if plain:
                cur.insertText(new_block)
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
            text = self.editor.mail_plain()  # цитата — строками «> », как принято в обычном тексте
            self.editor.edit.setAcceptRichText(False)
            self.editor.edit.setPlainText(text)
        else:
            self.editor.edit.setAcceptRichText(True)
        self.editor.set_formatting_visible(not plain)

    # ---------- вложения ----------
    def _attach(self):
        files, _ = QFileDialog.getOpenFileNames(self, tr("Вложить файлы"))
        self._add_files(files)

    def _add_files(self, files):
        failed = []
        for f in files:
            try:
                self.attachments.append((Path(f).name, Path(f).read_bytes()))
            except OSError as error:
                failed.append(f"{Path(f).name}: {error.strerror or error}")
        if files:
            self._refresh_attachments()
        if failed:
            QMessageBox.warning(self, tr("Вложения"), tr("Не удалось вложить файлы:\n{0}", "\n".join(failed)))

    # ---------- перетаскивание файлов ----------
    def _setup_file_drop(self):
        """Файлы из Проводника можно бросить в любое место окна — они станут вложениями.

        Картинки, брошенные прямо в текст письма с оформлением, вставляются в текст, как раньше.
        Поля окна (адреса, тема, текст) сами принимают перетаскивание, поэтому файлы
        перехватываются фильтром событий раньше них.
        """
        self.setAcceptDrops(True)
        self._drop_overlay = DropOverlay(self)
        for widget in self.findChildren(QWidget):
            if widget is not self._drop_overlay and widget.acceptDrops():
                widget.installEventFilter(self)

    def _inline_drop_rect(self, files) -> QRect | None:
        """Где в окне зона «вставить в текст» (только для картинок в письме с оформлением)."""
        if self.mode.currentData() != "html" or not all_inline_images(files):
            return None
        edit = self.editor.edit
        return QRect(edit.mapTo(self, QPoint(0, 0)), edit.size())

    def eventFilter(self, obj, event):
        if event.type() in DRAG_EVENTS and isinstance(obj, QWidget) and obj.window() is self:
            return self._file_drag(obj, event)
        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event):
        self._file_drag(self, event)

    def dragMoveEvent(self, event):
        self._file_drag(self, event)

    def dragLeaveEvent(self, event):
        self._file_drag(self, event)

    def dropEvent(self, event):
        self._file_drag(self, event)

    def _file_drag(self, widget: QWidget, event) -> bool:
        """True — событие обработано окном; False — пусть его обработает само поле."""
        if event.type() == QEvent.DragLeave:
            self._drop_overlay.hide()
            return False
        files = dropped_files(event.mimeData()) if self.isEnabled() else []
        if not files:
            self._drop_overlay.hide()
            return False
        inline_rect = self._inline_drop_rect(files)
        pos = widget.mapTo(self, event.position().toPoint())
        zone = ZONE_INLINE if inline_rect is not None and inline_rect.contains(pos) else ZONE_ATTACH
        # Всегда «копировать»: при «переместить» Проводник мог бы удалить исходный файл.
        event.setDropAction(Qt.CopyAction)
        event.accept()
        if event.type() != QEvent.Drop:
            self._drop_overlay.show_zones(inline_rect, zone)
            return True
        self._drop_overlay.hide()
        # Файлы читаются после того, как Проводник отпустит перетаскивание.
        if zone == ZONE_INLINE:
            edit = self.editor.edit
            edit.setTextCursor(edit.cursorForPosition(edit.viewport().mapFrom(self, pos)))
            QTimer.singleShot(0, lambda: self._insert_images(files))
        else:
            QTimer.singleShot(0, lambda: self._add_files(files))
        return True

    def _insert_images(self, files):
        for f in files:
            try:
                data = Path(f).read_bytes()
            except OSError as error:
                QMessageBox.warning(self, tr("Картинка"), tr("Не удалось открыть картинку «{0}».", Path(f).name)
                                    + f"\n{error.strerror or error}")
                continue
            self.editor._insert_image_data(data, image_type(f), Path(f).name)

    def _refresh_attachments(self):
        if hasattr(self, "_autosave"):
            self._schedule_save()
        self.att_list.clear()
        for index, (name, data) in enumerate(self.attachments):
            size = len(data)
            human = tr("{0:.1f} МБ", size / 1048576) if size > 1048576 else tr("{0} КБ", max(1, size // 1024))
            item = QListWidgetItem()
            item.setToolTip(name)
            chip = self._attachment_chip(f"{name} ({human})", index)
            item.setSizeHint(chip.sizeHint())
            self.att_list.addItem(item)
            self.att_list.setItemWidget(item, chip)
        self.att_list.setVisible(bool(self.attachments))

    def _attachment_chip(self, text: str, index: int) -> QWidget:
        """Плашка вложения: скрепка, имя и размер, крестик «убрать»."""
        chip = QWidget()
        lay = QHBoxLayout(chip); lay.setContentsMargins(4, 0, 6, 0); lay.setSpacing(4)
        clip = QLabel(); clip.setPixmap(icon("paperclip").pixmap(16, 16))
        label = QLabel(text)
        remove = QToolButton()
        remove.setIcon(icon("x")); remove.setIconSize(QSize(14, 14)); remove.setAutoRaise(True)
        remove.setToolTip(tr("Убрать вложение"))
        remove.setCursor(Qt.PointingHandCursor)
        # Плашка удаляется вместе со списком — не внутри нажатия её же кнопки.
        remove.clicked.connect(lambda: QTimer.singleShot(0, lambda: self._remove_attachment(index)))
        for widget in (clip, label, remove):
            lay.addWidget(widget)
        return chip

    def _remove_attachment(self, index: int):
        if 0 <= index < len(self.attachments):
            del self.attachments[index]
            self._refresh_attachments()

    def _attachment_menu(self, pos):
        menu = self._build_attachment_menu(pos)
        if not menu.isEmpty():
            menu.exec(self.att_list.viewport().mapToGlobal(pos))

    def _build_attachment_menu(self, pos) -> QMenu:
        item = self.att_list.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            row = self.att_list.row(item)
            menu.addAction(icon("x"), tr("Убрать вложение"), lambda: self._remove_attachment(row))
        if self.attachments:
            menu.addAction(icon("trash-2"), tr("Убрать все вложения"), self._remove_all_attachments)
        return menu

    def _remove_all_attachments(self):
        self.attachments.clear()
        self._refresh_attachments()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.att_list.hasFocus() and self.att_list.currentRow() >= 0:
            self._remove_attachment(self.att_list.currentRow())
            return
        super().keyPressEvent(event)

    # ---------- отправка ----------
    def _send(self):
        aid = self.account.currentData()
        if aid is None:
            QMessageBox.warning(self, tr("Отправка"), tr("Сначала добавьте почтовый ящик."))
            return
        if not (self.to.addresses_text() or self.cc.addresses_text() or self.bcc.addresses_text()):
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
            reason = (self.db.message(self._draft_id).delivery_error or "") if self._draft_id else ""
            if result == "failed":
                # Сервер отказал навсегда (например, нет такого адресата): письмо остаётся
                # в этом окне, чтобы исправить адрес и отправить снова.
                self.db.update_message(self._draft_id, folder="drafts", delivery_state=None, delivery_error=None)
                self.send_act.setEnabled(True)
                self.send_act.setText(tr("Отправить"))
                QMessageBox.warning(self, tr("Письмо не отправлено"),
                                    tr("Сервер отказался принять письмо.\n\n{0}\n\nИсправьте адреса и отправьте снова.", reason))
                self.saved.emit()
                return
            if result == "queued":
                QMessageBox.information(self, tr("Исходящие"), tr("Письмо сохранено в исходящих и будет отправлено при подключении.")
                                        + (tr("\n\nПричина: {0}", reason) if reason else ""))
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
