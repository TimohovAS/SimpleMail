"""Адресная книга и подсказки адресов в полях «Кому», «Копия», «Скрытая».

Книга заполняется сама: адресаты отправленных писем и отправители входящих (кроме спама
и служебных адресов). Контакты можно добавить, исправить и удалить вручную; удалённый
адрес сам больше не появляется, пока вы снова не напишете на него.
"""
from __future__ import annotations

from ..i18n import tr

from datetime import datetime

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QCompleter, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..contacts import format_address, parse_addresses, valid_email
from ..db import Contact, Database
from .icons import icon

SEPARATORS = ",;"


def _token_bounds(text: str, cursor: int) -> tuple[int, int]:
    """Границы адреса под курсором: от предыдущей запятой (;) до следующей.
    Запятые внутри кавычек («"Петров, Иван" <a@b.c>») адреса не разделяют."""
    bounds, start, quoted = [], 0, False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif ch in SEPARATORS and not quoted:
            bounds.append((start, i))
            start = i + 1
    bounds.append((start, len(text)))
    for start, end in bounds:
        if start <= cursor <= end:
            return start, end
    return bounds[-1]


class AddressEdit(QLineEdit):
    """Поле адресов: при вводе подсказывает адреса из книги; Enter или Tab подставляет выбранный."""

    def __init__(self, db: Database, text: str = "", parent=None):
        super().__init__(text, parent)
        self.db = db
        self._model = QStandardItemModel(self)
        self.completer_ = QCompleter(self._model, self)
        self.completer_.setWidget(self)
        self.completer_.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer_.setMaxVisibleItems(8)
        self.completer_.activated[QModelIndex].connect(self._insert)
        self.textEdited.connect(self._suggest)
        self.setToolTip(tr("Начните вводить имя или адрес — появятся подсказки из адресной книги. "
                           "Несколько адресов разделяйте запятой"))

    def _current_token(self) -> tuple[int, int, str]:
        start, end = _token_bounds(self.text(), self.cursorPosition())
        return start, end, self.text()[start:end]

    def _suggest(self, *_):
        _, _, token = self._current_token()
        token = token.strip()
        popup = self.completer_.popup()
        if not token or "<" in token:
            popup.hide()
            return
        others = [address for _, address in parse_addresses(self.text())
                  if address.casefold() != token.casefold()]
        found = self.db.suggest_contacts(token, limit=8, exclude=others)
        self._model.clear()
        for contact in found:
            item = QStandardItem(format_address(contact.name, contact.email))
            item.setData(format_address(contact.name, contact.email), Qt.UserRole)
            item.setToolTip(contact.email)
            self._model.appendRow(item)
        if not found:
            popup.hide()
            return
        self.completer_.complete()
        popup.setCurrentIndex(self.completer_.completionModel().index(0, 0))

    def _insert(self, index: QModelIndex):
        value = index.data(Qt.UserRole)
        if not value:
            return
        self.insert_address(value)

    def insert_address(self, value: str):
        """Подставить адрес вместо набираемого и поставить «, » после него — можно вводить следующий."""
        text = self.text()
        start, end, _ = self._current_token()
        before = text[:start].rstrip()
        after = text[end:].lstrip()
        if after[:1] in SEPARATORS and after:
            after = after[1:].lstrip()
        new = (before + " " if before else "") + value + ", "
        self.setText(new + after)
        self.setCursorPosition(len(new))
        self.completer_.popup().hide()

    def add_addresses(self, values: list[str]):
        """Дописать адреса в конец поля (из адресной книги), пропуская уже указанные."""
        have = {address.casefold() for _, address in parse_addresses(self.text())}
        added = []
        for value in values:
            people = parse_addresses(value)
            if people and people[0][1].casefold() not in have:
                have.add(people[0][1].casefold())
                added.append(value)
        if added:
            base = self.addresses_text()
            self.setText(", ".join(([base] if base else []) + added))

    def addresses_text(self) -> str:
        """Текст поля без висящей в конце запятой."""
        return self.text().strip().rstrip(SEPARATORS + " ")

    def keyPressEvent(self, event):
        popup = self.completer_.popup()
        if popup.isVisible():
            if event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab):
                index = popup.currentIndex()
                if index.isValid() and not event.modifiers() & Qt.ControlModifier:
                    self._insert(index)
                    return
                popup.hide()
            elif event.key() == Qt.Key_Escape:
                popup.hide()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # Убираем висящую в конце запятую, чтобы поле выглядело аккуратно.
        if not self.completer_.popup().isVisible() and self.addresses_text() != self.text():
            self.setText(self.addresses_text())


class ContactDialog(QDialog):
    """Добавление или правка одного контакта."""

    def __init__(self, db: Database, contact: Contact | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.contact = contact or Contact()
        self.setWindowTitle(tr("Изменить контакт") if contact and contact.id else tr("Новый контакт"))
        self.name = QLineEdit(self.contact.name)
        self.name.setPlaceholderText(tr("Например, Иван Петров"))
        self.email = QLineEdit(self.contact.email)
        self.email.setPlaceholderText("name@example.com")
        form = QFormLayout()
        form.addRow(tr("Имя:"), self.name)
        form.addRow(tr("Адрес:"), self.email)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText(tr("Сохранить"))
        bb.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self.resize(420, self.sizeHint().height())
        (self.email if self.contact.name or not self.contact.email else self.name).setFocus()

    def _save(self):
        address = self.email.text().strip()
        if address.startswith("<") and address.endswith(">"):
            address = address[1:-1].strip()
        if not valid_email(address):
            QMessageBox.warning(self, tr("Адресная книга"), tr("Неверный адрес: «{0}».", address))
            self.email.setFocus()
            return
        self.contact.email, self.contact.name = address, self.name.text().strip()
        try:
            self.db.save_contact(self.contact)
        except ValueError:
            QMessageBox.warning(self, tr("Адресная книга"), tr("Адрес {0} уже есть в адресной книге.", address))
            self.email.setFocus()
            return
        self.accept()


class AddressBookDialog(QDialog):
    """Адресная книга. picker=True — открыта из письма: кнопки «Кому», «Копия», «Скрытая»."""

    picked = Signal(str, list)        # поле (to | cc | bcc), адреса «Имя <адрес>»
    compose_requested = Signal(str)   # «Написать письмо» выбранным

    def __init__(self, db: Database, picker: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.picker = picker
        self.setWindowTitle(tr("Адресная книга"))
        self.resize(820, 520)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Поиск по имени или адресу"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.reload)

        self.list = QTreeWidget()
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setHeaderLabels([tr("Имя"), tr("Адрес"), tr("Писем им"), tr("Писем от них"), tr("Последнее письмо")])
        self.list.headerItem().setToolTip(2, tr("Сколько писем вы отправили на этот адрес"))
        self.list.headerItem().setToolTip(3, tr("Сколько писем пришло с этого адреса"))
        self.list.setSortingEnabled(True)
        self.list.sortByColumn(0, Qt.AscendingOrder)
        header = self.list.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        for column, width in enumerate((220, 250, 80, 95)):
            header.resizeSection(column, width)
        self.list.itemDoubleClicked.connect(lambda *_: self._default_action())
        self.list.itemSelectionChanged.connect(self._update_buttons)

        self.add_btn = QPushButton(icon("plus"), tr("Добавить"))
        self.add_btn.clicked.connect(self._add)
        self.edit_btn = QPushButton(tr("Изменить"))
        self.edit_btn.clicked.connect(self._edit)
        self.del_btn = QPushButton(icon("trash-2"), tr("Удалить"))
        self.del_btn.clicked.connect(self._delete)
        self.del_btn.setShortcut(QKeySequence.Delete)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.edit_btn)
        buttons.addWidget(self.del_btn)
        buttons.addStretch(1)
        self.pick_buttons = []
        if picker:
            for field, title in (("to", tr("Кому")), ("cc", tr("Копия")), ("bcc", tr("Скрытая"))):
                b = QPushButton(title)
                b.setToolTip(tr("Добавить выбранные адреса в поле «{0}»", title))
                b.clicked.connect(lambda _=False, f=field: self._pick(f))
                buttons.addWidget(b)
                self.pick_buttons.append(b)
        else:
            self.compose_btn = QPushButton(icon("mail-plus"), tr("Написать письмо"))
            self.compose_btn.clicked.connect(self._compose)
            buttons.addWidget(self.compose_btn)
            self.pick_buttons.append(self.compose_btn)
        close = QPushButton(tr("Закрыть"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)

        self.hint = QLabel(tr("Адреса добавляются сами: кому вы писали и кто писал вам (кроме спама и рассылок "
                              "вида noreply). Удалённый адрес сам больше не появится."))
        self.hint.setWordWrap(True)
        self.hint.setProperty("tone", "muted")
        self.count = QLabel()
        self.count.setProperty("tone", "muted")

        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.list, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.hint, 1)
        bottom.addWidget(self.count)
        lay.addLayout(bottom)
        lay.addLayout(buttons)
        self.reload()
        self.search.setFocus()

    # ---------- список ----------
    def reload(self, *_):
        selected = {c.id for c in self.selected()}
        self.list.setSortingEnabled(False)
        self.list.clear()
        contacts = self.db.contacts(self.search.text())
        for contact in contacts:
            last = datetime.fromtimestamp(contact.last_ts).strftime("%d.%m.%Y") if contact.last_ts else ""
            item = _ContactItem([contact.name, contact.email, str(contact.sent_count or ""),
                                 str(contact.received_count or ""), last])
            item.setData(0, Qt.UserRole, contact)
            item.setData(4, Qt.UserRole, contact.last_ts)
            for column in (2, 3):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            self.list.addTopLevelItem(item)
            item.setSelected(contact.id in selected)
        self.list.setSortingEnabled(True)
        self.list.sortItems(self.list.sortColumn(), self.list.header().sortIndicatorOrder())
        self.count.setText(tr("Контактов: {0}", len(contacts)))
        if self.list.currentItem() is None and self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0), 0)
            if not selected:
                self.list.clearSelection()
        self._update_buttons()

    def selected(self) -> list[Contact]:
        return [item.data(0, Qt.UserRole) for item in self.list.selectedItems()]

    def _update_buttons(self):
        count = len(self.list.selectedItems())
        self.edit_btn.setEnabled(count == 1)
        self.del_btn.setEnabled(count > 0)
        for b in self.pick_buttons:
            b.setEnabled(count > 0)

    def _addresses(self) -> list[str]:
        return [format_address(c.name, c.email) for c in self.selected()]

    # ---------- действия ----------
    def _default_action(self):
        if self.picker:
            self._pick("to")
        else:
            self._edit()

    def _add(self):
        dlg = ContactDialog(self.db, Contact(email=self.search.text().strip() if "@" in self.search.text() else ""), self)
        if dlg.exec():
            self.search.clear()
            self.reload()
            self._select(dlg.contact.id)

    def _edit(self):
        items = self.selected()
        if len(items) != 1:
            return
        dlg = ContactDialog(self.db, items[0], self)
        if dlg.exec():
            self.reload()
            self._select(dlg.contact.id)

    def _select(self, contact_id):
        self.list.clearSelection()
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if item.data(0, Qt.UserRole).id == contact_id:
                self.list.setCurrentItem(item)
                item.setSelected(True)
                self.list.scrollToItem(item)
                break

    def _delete(self):
        items = self.selected()
        if not items:
            return
        text = (tr("Удалить {0} из адресной книги?", format_address(items[0].name, items[0].email)) if len(items) == 1
                else tr("Удалить выбранные контакты ({0}) из адресной книги?", len(items)))
        if QMessageBox.question(self, tr("Адресная книга"), text) != QMessageBox.Yes:
            return
        self.db.delete_contacts([c.id for c in items])
        self.reload()

    def _pick(self, field: str):
        if self.selected():
            self.picked.emit(field, self._addresses())

    def _compose(self):
        if self.selected():
            self.compose_requested.emit(", ".join(self._addresses()))
            self.accept()


class _ContactItem(QTreeWidgetItem):
    """Числа и даты сортируются как числа и даты, а не как текст."""

    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column in (2, 3):
            return int(self.text(column) or 0) < int(other.text(column) or 0)
        if column == 4:
            return (self.data(4, Qt.UserRole) or 0) < (other.data(4, Qt.UserRole) or 0)
        if column == 0:  # контакт без имени стоит среди остальных по адресу
            return (self.text(0) or self.text(1)).casefold() < (other.text(0) or other.text(1)).casefold()
        return self.text(column).casefold() < other.text(column).casefold()


def ask_addresses(db: Database, parent, title: str, label: str) -> str:
    """Спросить адреса (с подсказками из книги). Пустая строка — отмена."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    edit = AddressEdit(db)
    text = QLabel(label)
    text.setWordWrap(True)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Cancel).setText(tr("Отмена"))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay = QVBoxLayout(dlg)
    lay.addWidget(text)
    lay.addWidget(edit)
    lay.addWidget(bb)
    dlg.resize(480, dlg.sizeHint().height())
    edit.setFocus()
    return edit.addresses_text() if dlg.exec() else ""
